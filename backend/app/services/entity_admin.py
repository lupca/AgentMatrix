"""Service-layer CRUD for Project/Agent/KnowledgeItem.

Shared by the REST endpoints (``app/api/projects.py``, ``app/api/agents.py``,
``app/api/knowledge.py``) and the coordinator ``manage_project``/``manage_agent``/
``manage_knowledge`` tools (ADR-001 §D2) so every entry point enforces the same
validation and never duplicates it. No entity here supports hard delete —
"archive"/"disable" are status changes.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    Agent,
    AgentCapability,
    AgentCapabilityLink,
    AgentRole,
    AgentRoleLink,
    KnowledgeItem,
    Project,
    Setting,
)
from app.graph.context import invalidate_context_snapshot
from app.services.crypto import encrypt_api_key


class EntityError(RuntimeError):
    """Base error for a rejected entity-admin operation."""


class EntityNotFoundError(EntityError):
    pass


class EntityConflictError(EntityError):
    pass


class EntityValidationError(EntityError):
    pass


# --- Projects ---------------------------------------------------------------

_PROJECT_FIELDS = {
    "description",
    "context_md",
    "status",
    "repo_root",
    "task_prefix",
    "autonomy_policy",
}


def create_project(db: Session, data: dict[str, Any]) -> Project:
    project_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    if not project_id or not name:
        raise EntityValidationError("id and name are required")
    if db.query(Project).filter(Project.id == project_id).first() is not None:
        raise EntityConflictError(f"Project with ID '{project_id}' already exists.")

    fields = {k: v for k, v in data.items() if k in _PROJECT_FIELDS}
    fields["id"] = project_id
    fields["name"] = name
    fields.setdefault("status", "active")
    project = Project(**fields)
    db.add(project)
    db.commit()
    db.refresh(project)
    invalidate_context_snapshot(db, project_id=project.id)
    return project


def update_project(db: Session, project_id: str, data: dict[str, Any]) -> Project:
    project = db.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise EntityNotFoundError(f"Project '{project_id}' not found.")

    for field, value in data.items():
        if field in _PROJECT_FIELDS or field == "name":
            setattr(project, field, value)

    db.commit()
    db.refresh(project)
    invalidate_context_snapshot(db, project_id=project.id)
    return project


def archive_project(db: Session, project_id: str) -> Project:
    return update_project(db, project_id, {"status": "archived"})


# --- Agents ------------------------------------------------------------------

_AGENT_UPDATE_FIELDS = {
    "name",
    "role",
    "roles",
    "capabilities",
    "status",
    "type",
    "model",
    "effort",
    "cli",
    "agent_type",
    "provider",
    "is_default",
}
_AGENT_CREATE_FIELDS = _AGENT_UPDATE_FIELDS


def _validate_agent_configuration(
    agent_type: str,
    cli: str | None,
    provider: str | None,
    has_api_key: bool,
    require_cli: bool = True,
) -> None:
    if agent_type == "api":
        if not provider:
            raise EntityValidationError("API agents require a provider.")
        if not has_api_key:
            raise EntityValidationError("API agents require an api_key.")
    elif agent_type == "cli" and require_cli and not cli:
        raise EntityValidationError("CLI agents require a cli tool.")


def _validate_default_role(role: str, is_default: bool) -> None:
    if is_default and role != "coordinator":
        raise EntityValidationError(
            "Only coordinator agents can be the default coordinator."
        )


def _normalized_roles(data: dict[str, Any], fallback: list[str] | None = None) -> list[str]:
    roles = data.get("roles")
    if roles is None:
        roles = [data.get("role")] if data.get("role") is not None else (fallback or [])
    # Normalize to lowercase to match enum values (handles uppercase enum names from API)
    result = list(dict.fromkeys(str(value).strip().lower() for value in roles if str(value).strip()))
    invalid = set(result) - {item.value for item in AgentRole}
    if invalid:
        raise EntityValidationError(f"Unknown agent role(s): {', '.join(sorted(invalid))}")
    return result


def _normalized_capabilities(data: dict[str, Any], fallback: list[str] | None = None) -> list[str]:
    values = data.get("capabilities", fallback or [])
    # Normalize to lowercase to match enum values (handles uppercase enum names from API)
    result = list(dict.fromkeys(str(value).strip().lower() for value in (values or []) if str(value).strip()))
    invalid = set(result) - {item.value for item in AgentCapability}
    if invalid:
        raise EntityValidationError(f"Unknown agent capability(s): {', '.join(sorted(invalid))}")
    return result


def _sync_agent_links(db: Session, agent: Agent, roles: list[str], capabilities: list[str]) -> None:
    agent.agent_roles.clear()
    agent.agent_capabilities.clear()
    db.flush()
    agent.agent_roles.extend(AgentRoleLink(role=AgentRole(value)) for value in roles)
    agent.agent_capabilities.extend(
        AgentCapabilityLink(capability=AgentCapability(value)) for value in capabilities
    )


def unset_coordinator_defaults(db: Session, except_id: str | None = None) -> None:
    query = db.query(Agent).filter(Agent.role == "coordinator")
    if except_id:
        query = query.filter(Agent.id != except_id)
    query.update({Agent.is_default: False}, synchronize_session=False)


def create_agent(db: Session, data: dict[str, Any]) -> Agent:
    agent_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    roles = _normalized_roles(data)
    role = roles[0] if roles else ""
    if not agent_id or not name or not role:
        raise EntityValidationError("id, name, and at least one role are required")
    capabilities = _normalized_capabilities(data)
    if db.query(Agent).filter(Agent.id == agent_id).first() is not None:
        raise EntityConflictError(f"Agent with ID '{agent_id}' already exists.")

    has_explicit_type = "agent_type" in data
    agent_type = data.get("agent_type", "cli")
    api_key = data.get("api_key")
    # The manage_agent tool encrypts before the payload is persisted to the
    # append-only admin-gate ledger; the REST path passes plaintext api_key.
    api_key_encrypted = data.get("api_key_encrypted")
    base_url = data.get("base_url")

    _validate_agent_configuration(
        agent_type,
        data.get("cli"),
        data.get("provider"),
        bool(api_key or api_key_encrypted),
        require_cli=has_explicit_type,
    )

    fields = {k: v for k, v in data.items() if k in _AGENT_CREATE_FIELDS and k != "roles"}
    fields["id"] = agent_id
    fields["name"] = name
    fields["role"] = role
    fields["capabilities"] = capabilities
    fields["agent_type"] = agent_type
    if agent_type == "api":
        fields["api_key"] = api_key_encrypted or encrypt_api_key(api_key)
        fields["base_url"] = (base_url or "").strip() or None
    else:
        fields["api_key"] = None
        fields["provider"] = None
        fields["base_url"] = None

    _validate_default_role(role, bool(fields.get("is_default", False)))
    if role != "coordinator":
        fields["is_default"] = False
    if fields.get("is_default"):
        unset_coordinator_defaults(db)

    agent = Agent(**fields)
    db.add(agent)
    db.flush()
    _sync_agent_links(db, agent, roles, capabilities)
    db.commit()
    db.refresh(agent)
    invalidate_context_snapshot(db)
    return agent


def update_agent(db: Session, agent_id: str, data: dict[str, Any]) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        raise EntityNotFoundError(f"Agent '{agent_id}' not found.")

    # Drop unknown keys (notably "id") before they can reach the setattr loop
    # below — callers passing a raw dict (manage_agent tool) aren't fenced in
    # by a pydantic schema the way the REST path's AgentUpdate model is.
    patch = {
        k: v
        for k, v in data.items()
        if k in _AGENT_UPDATE_FIELDS or k in {"api_key", "api_key_encrypted", "base_url"}
    }
    if not patch and not ("api_key" in data or "api_key_encrypted" in data):
        raise EntityValidationError(
            "Agent update contains no updatable fields; allowed: "
            + ", ".join(sorted(_AGENT_UPDATE_FIELDS | {"api_key", "base_url"}))
        )
    api_key_was_provided = "api_key" in patch or "api_key_encrypted" in patch
    api_key = patch.pop("api_key", None)
    api_key_encrypted = patch.pop("api_key_encrypted", None)
    if "base_url" in patch:
        patch["base_url"] = (patch["base_url"] or "").strip() or None

    target_type = patch.get("agent_type", agent.agent_type or "cli")
    target_cli = patch.get("cli", agent.cli)
    target_provider = patch.get("provider", agent.provider)
    has_api_key = (
        bool(api_key or api_key_encrypted) if api_key_was_provided else bool(agent.api_key)
    )
    legacy_cli_update = (
        target_type == "cli"
        and not agent.cli
        and "agent_type" not in patch
        and "cli" not in patch
    )
    _validate_agent_configuration(
        target_type,
        target_cli,
        target_provider,
        has_api_key,
        require_cli=not legacy_cli_update,
    )

    if target_type == "api" and api_key_was_provided:
        patch["api_key"] = api_key_encrypted or encrypt_api_key(api_key)
    elif target_type == "cli":
        patch["api_key"] = None
        patch["provider"] = None
        patch["base_url"] = None

    roles = _normalized_roles(patch, fallback=agent.normalized_roles)
    target_role = roles[0] if roles else agent.role
    capabilities = _normalized_capabilities(patch, fallback=agent.normalized_capabilities)
    target_default = patch.get("is_default", agent.is_default)
    _validate_default_role(target_role, target_default)
    if target_role != "coordinator":
        patch["is_default"] = False
    if patch.get("is_default"):
        unset_coordinator_defaults(db, except_id=agent_id)

    for field, value in patch.items():
        if field in {"roles", "capabilities"}:
            continue
        setattr(agent, field, value)

    agent.role = target_role
    agent.capabilities = capabilities
    _sync_agent_links(db, agent, roles, capabilities)

    db.commit()
    db.refresh(agent)
    invalidate_context_snapshot(db)
    return agent


def disable_agent(db: Session, agent_id: str) -> Agent:
    return update_agent(db, agent_id, {"status": "disabled"})


# --- Knowledge -----------------------------------------------------------------

_KNOWLEDGE_FIELDS = {"title", "category", "content", "tags", "project", "author", "status"}


def create_knowledge(db: Session, data: dict[str, Any]) -> KnowledgeItem:
    title = str(data.get("title", "")).strip()
    if not title:
        raise EntityValidationError("title is required")

    requested_id = str(data.get("id") or "").strip()
    if requested_id:
        if db.query(KnowledgeItem).filter(KnowledgeItem.id == requested_id).first() is not None:
            raise EntityConflictError(
                f"Knowledge item with ID '{requested_id}' already exists."
            )
        item_id = requested_id
    else:
        item_id = f"k-{uuid.uuid4().hex[:8]}"

    fields = {k: v for k, v in data.items() if k in _KNOWLEDGE_FIELDS}
    fields["id"] = item_id
    fields["title"] = title
    fields.setdefault("status", "active")
    item = KnowledgeItem(**fields)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_knowledge(db: Session, item_id: str, data: dict[str, Any]) -> KnowledgeItem:
    item = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    if item is None:
        raise EntityNotFoundError(f"Knowledge item '{item_id}' not found.")

    for field, value in data.items():
        if field in _KNOWLEDGE_FIELDS:
            setattr(item, field, value)

    db.commit()
    db.refresh(item)
    return item


def archive_knowledge(db: Session, item_id: str) -> KnowledgeItem:
    return update_knowledge(db, item_id, {"status": "archived"})


# --- Settings ------------------------------------------------------------

# Keys `update_settings` is allowed to write. Grow this dict, not the schema,
# when a new setting is needed — no migration required.
SETTINGS_WHITELIST: dict[str, str] = {
    "default_coordinator_model": "Default model used for new coordinator sessions.",
    "default_mode": "Default gate mode (supervised/plan-only/bypass) for new tasks.",
    "context_snapshot_top_n": "Number of recent tasks listed in the context snapshot.",
    "autonomy_enabled": "Global kill switch for autonomous task progression.",
    "max_cost_usd_per_task": "Maximum accumulated LLM cost allowed for one task.",
    "max_concurrent_runs": "Maximum number of queued or running agent runs.",
    "run_timeout_seconds": "Maximum wall-clock time for one agent run.",
    "max_active_seconds_per_run": "Maximum active execution time for one agent run.",
    "max_tool_calls_per_run": "Maximum tool calls allowed for one agent run.",
    "max_no_progress_seconds": "Maximum time without progress for one agent run.",
    "autonomy": "Task autonomy policy: plan-only, supervised, or auto.",
    "auto_max_risk": "Highest task risk eligible for automatic execution: low or normal.",
    "auto_max_rounds": "Maximum changes-requested replan rounds before escalation.",
    "sql_timeout_seconds": "Statement execution timeout for query_db SQL queries.",
    "sql_row_cap": "Maximum row count returned by query_db SQL queries.",
    "embedding_api_url": "Base URL for the text embedding API.",
    "embedding_api_key": "API key for the text embedding API.",
    "embedding_model": "Model used by the text embedding API.",
}


def update_setting(db: Session, key: str, value: Any) -> Setting:
    if key not in SETTINGS_WHITELIST:
        raise EntityValidationError(
            f"Unknown setting key '{key}'. Allowed keys: "
            f"{', '.join(sorted(SETTINGS_WHITELIST))}"
        )

    setting = db.get(Setting, key)
    if setting is None:
        setting = Setting(key=key, description=SETTINGS_WHITELIST[key])
        db.add(setting)
    setting.value = value
    db.commit()
    db.refresh(setting)
    return setting
