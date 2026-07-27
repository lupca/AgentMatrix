"""Gate ledger for admin-permission entity mutations (ADR-001 §D2).

Mirrors ``TaskOrchestrationService``'s pending/decide pattern but for
mutations that are not scoped to a Task: ``manage_project`` and
``manage_agent`` are permission=admin tools, so in ``supervised`` mode a
request only records a pending :class:`AdminGateRecord` (no mutation yet) and
requires ``/approve``; in ``bypass`` mode the mutation applies immediately.
Every outcome — pending, approved, or rejected — is written to ``AuditLog``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.db.models import AdminGateRecord, AuditLog
from app.services import entity_admin

GateDecision = Literal["approved", "rejected"]

_MODES = {"supervised", "bypass"}

_ENTITY_ACTIONS: dict[str, set[str]] = {
    "projects": {"create", "update", "archive"},
    "agents": {"create", "update", "disable"},
}

_REQUIRED_CREATE_FIELDS: dict[str, set[str]] = {
    "projects": {"id", "name"},
    "agents": {"id", "name", "role"},
}


class AdminOrchestrationError(RuntimeError):
    """Base error for a rejected admin-gate operation."""


class AdminTransitionConflictError(AdminOrchestrationError):
    pass


class AdminPrerequisiteError(AdminOrchestrationError):
    pass


@dataclass(frozen=True)
class AdminTransitionResult:
    record: AdminGateRecord
    applied: bool
    entity_id: str | None = None
    output: dict[str, Any] | None = None


class AdminGateService:
    """The only application service allowed to apply admin-permission entity mutations."""

    def __init__(self, db: Session):
        self.db = db

    def request(
        self,
        *,
        entity: str,
        action: str,
        actor: str,
        mode: str,
        payload: dict[str, Any],
        entity_id: str | None = None,
    ) -> AdminTransitionResult:
        self._validate_request(entity, action, actor, mode, entity_id, payload)

        if mode == "supervised":
            record = AdminGateRecord(
                entity=entity,
                action=action,
                entity_id=entity_id,
                status="pending",
                actor=actor,
                mode=mode,
                input_payload=payload,
            )
            self.db.add(record)
            self._audit(record)
            self.db.commit()
            self.db.refresh(record)
            return AdminTransitionResult(record=record, applied=False)

        applied_id, output = self._apply(entity, action, entity_id, payload)
        record = AdminGateRecord(
            entity=entity,
            action=action,
            entity_id=applied_id,
            status="approved",
            actor=actor,
            mode=mode,
            input_payload=payload,
            output_payload=output,
        )
        self.db.add(record)
        self._audit(record)
        self.db.commit()
        self.db.refresh(record)
        return AdminTransitionResult(
            record=record, applied=True, entity_id=applied_id, output=output
        )

    def decide(
        self,
        *,
        admin_gate_id: int,
        decision: GateDecision,
        actor: str,
    ) -> AdminTransitionResult:
        if decision not in {"approved", "rejected"}:
            raise AdminPrerequisiteError("Decision must be approved or rejected")
        pending = self.db.get(AdminGateRecord, admin_gate_id)
        if pending is None or pending.status != "pending":
            raise AdminTransitionConflictError(
                f"Pending admin gate record {admin_gate_id} not found"
            )

        applied_id: str | None = pending.entity_id
        output: dict[str, Any] | None = None
        if decision == "approved":
            applied_id, output = self._apply(
                pending.entity,
                pending.action,
                pending.entity_id,
                pending.input_payload or {},
            )

        record = AdminGateRecord(
            entity=pending.entity,
            action=pending.action,
            entity_id=applied_id,
            status=decision,
            actor=actor,
            mode=pending.mode,
            parent_id=pending.id,
            input_payload=pending.input_payload,
            output_payload=output,
        )
        self.db.add(record)
        self._audit(record)
        self.db.commit()
        self.db.refresh(record)
        return AdminTransitionResult(
            record=record,
            applied=decision == "approved",
            entity_id=applied_id,
            output=output,
        )

    def _validate_request(
        self,
        entity: str,
        action: str,
        actor: str,
        mode: str,
        entity_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        allowed_actions = _ENTITY_ACTIONS.get(entity)
        if allowed_actions is None:
            raise AdminPrerequisiteError(f"Unknown admin entity: {entity}")
        if action not in allowed_actions:
            raise AdminPrerequisiteError(
                f"Unknown action '{action}' for {entity}. Valid actions: "
                f"{', '.join(sorted(allowed_actions))}"
            )
        if mode not in _MODES:
            raise AdminPrerequisiteError(f"mode must be one of {sorted(_MODES)}")
        if not actor or not actor.strip():
            raise AdminPrerequisiteError("actor is required")
        if action != "create" and not entity_id:
            raise AdminPrerequisiteError(f"id is required for {action}")
        if action == "create":
            missing = _REQUIRED_CREATE_FIELDS.get(entity, set()) - set(payload)
            if missing:
                raise AdminPrerequisiteError(
                    f"Missing required fields: {', '.join(sorted(missing))}"
                )

    def _apply(
        self,
        entity: str,
        action: str,
        entity_id: str | None,
        payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        try:
            if entity == "projects":
                obj = self._apply_project(action, entity_id, payload)
                output = {"id": obj.id, "name": obj.name, "status": obj.status}
            elif entity == "agents":
                obj = self._apply_agent(action, entity_id, payload)
                output = {
                    "id": obj.id,
                    "name": obj.name,
                    "status": obj.status,
                    "has_api_key": obj.has_api_key,
                }
            else:
                raise AdminPrerequisiteError(f"Unknown admin entity: {entity}")
        except entity_admin.EntityError as exc:
            raise AdminPrerequisiteError(str(exc)) from exc
        return obj.id, output

    def _apply_project(self, action: str, entity_id: str | None, payload: dict[str, Any]):
        if action == "create":
            return entity_admin.create_project(self.db, payload)
        if action == "update":
            return entity_admin.update_project(self.db, entity_id, payload)
        return entity_admin.archive_project(self.db, entity_id)

    def _apply_agent(self, action: str, entity_id: str | None, payload: dict[str, Any]):
        if action == "create":
            return entity_admin.create_agent(self.db, payload)
        if action == "update":
            return entity_admin.update_agent(self.db, entity_id, payload)
        return entity_admin.disable_agent(self.db, entity_id)

    def _audit(self, record: AdminGateRecord) -> None:
        self.db.flush()
        self.db.add(
            AuditLog(
                task_id=None,
                action=f"admin_gate:{record.entity}:{record.action}:{record.status}",
                actor=record.actor,
                details={
                    "admin_gate_record_id": record.id,
                    "entity": record.entity,
                    "entity_id": record.entity_id,
                    "mode": record.mode,
                },
            )
        )
