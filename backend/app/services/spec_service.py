"""Transactional persistence and read projection for the living spec core."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import SpecAnchor, SpecItem, SpecRelation
from app.services.spec_anchor import compute_anchor_sha


SPEC_KINDS = {"requirement", "decision", "constraint", "interface", "design"}
SPEC_STATUSES = {"draft", "active", "stale", "superseded"}
SPEC_CONFIDENCES = {"asserted", "derived", "verified"}
RELATION_KINDS = {"conflicts_with", "duplicates", "refines", "depends_on"}
ANCHOR_RELATION_KINDS = {"implements", "constrains", "tests", "documents"}

_ITEM_FIELDS = {
    "project_id", "kind", "title", "body", "status", "supersedes_id",
    "source_doc_id", "derived_from_sha", "derived_by", "confidence",
    "verified_at", "verified_by", "embedding",
}
_FILTER_FIELDS = {
    "project_id", "kind", "status", "confidence", "derived_by",
    "source_doc_id", "supersedes_id",
}


class SpecError(ValueError):
    """A user-correctable living-spec request error."""


def _clean_string(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise SpecError(f"{field} is required")
        return None
    result = str(value).strip()
    if required and not result:
        raise SpecError(f"{field} is required")
    return result or None


def _validate_item_fields(fields: Mapping[str, Any], *, partial: bool) -> dict[str, Any]:
    unknown = set(fields) - _ITEM_FIELDS
    if unknown:
        raise SpecError(f"Unknown spec item field(s): {', '.join(sorted(unknown))}")
    values = {key: fields[key] for key in _ITEM_FIELDS if key in fields}
    for field in ("project_id", "kind", "title", "body"):
        if not partial or field in values:
            values[field] = _clean_string(values.get(field), field, required=True)
    if "kind" in values and values["kind"] not in SPEC_KINDS:
        raise SpecError(f"kind must be one of {sorted(SPEC_KINDS)}")
    for field in ("status", "confidence"):
        if field in values:
            values[field] = _clean_string(values[field], field, required=True)
            allowed = SPEC_STATUSES if field == "status" else SPEC_CONFIDENCES
            if values[field] not in allowed:
                raise SpecError(f"{field} must be one of {sorted(allowed)}")
    for field in ("supersedes_id", "source_doc_id", "derived_from_sha", "derived_by", "verified_by"):
        if field in values:
            values[field] = _clean_string(values[field], field)
    if "verified_at" in values and values["verified_at"] is not None:
        value = values["verified_at"]
        if isinstance(value, str):
            try:
                values["verified_at"] = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SpecError("verified_at must be an ISO-8601 timestamp") from exc
        elif not isinstance(value, datetime):
            raise SpecError("verified_at must be an ISO-8601 timestamp")
    if "embedding" in values and values["embedding"] is not None:
        if not isinstance(values["embedding"], Sequence) or isinstance(values["embedding"], (str, bytes)):
            raise SpecError("embedding must be an array")
        try:
            values["embedding"] = [float(value) for value in values["embedding"]]
        except (TypeError, ValueError) as exc:
            raise SpecError("embedding must contain numbers") from exc
    return values


def _active_item(db: Session, item_id: str) -> SpecItem | None:
    return db.query(SpecItem).filter(
        SpecItem.id == item_id, SpecItem.archived_at.is_(None)
    ).first()


def _resolve_item(db: Session, item_id: str, pending: Mapping[str, SpecItem]) -> SpecItem:
    item = pending.get(item_id) or _active_item(db, item_id)
    if item is None:
        raise SpecError(f"Spec item '{item_id}' not found")
    return item


def _operation_name(operation: Mapping[str, Any]) -> str:
    value = operation.get("op", operation.get("action"))
    aliases = {
        "add": "create", "edit": "update", "replace": "supersede",
        "relate": "relation", "link": "relation",
    }
    return aliases.get(str(value or "").strip().lower(), str(value or "").strip().lower())


def _item_payload(operation: Mapping[str, Any]) -> dict[str, Any]:
    nested = operation.get("item", operation.get("new_item", operation.get("patch")))
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise SpecError("item/patch must be an object")
        values = dict(nested)
        values.update({key: value for key, value in operation.items() if key in _ITEM_FIELDS})
        return values
    return {key: value for key, value in operation.items() if key in _ITEM_FIELDS}


def _item_snapshot(item: SpecItem, relations: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "id": item.id, "project_id": item.project_id, "kind": item.kind,
        "title": item.title, "body": item.body, "status": item.status,
        "supersedes_id": item.supersedes_id, "source_doc_id": item.source_doc_id,
        "derived_from_sha": item.derived_from_sha, "derived_by": item.derived_by,
        "confidence": item.confidence,
        "verified_at": item.verified_at.isoformat() if item.verified_at else None,
        "verified_by": item.verified_by,
        "archived_at": item.archived_at.isoformat() if item.archived_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "relations": relations or [],
    }


def _relation_snapshot(relation: SpecRelation) -> dict[str, str]:
    return {"from_id": relation.from_id, "to_id": relation.to_id, "kind": relation.kind}


def _anchor_snapshot(anchor: SpecAnchor) -> dict[str, str]:
    return {
        "id": anchor.id, "spec_item_id": anchor.spec_item_id, "repo": anchor.repo,
        "path": anchor.path, "symbol": anchor.symbol, "relation": anchor.relation,
        "anchor_sha": anchor.anchor_sha,
    }


def write_specs(
    db: Session,
    operations: list[Mapping[str, Any]],
    common_project_id: str | None = None,
) -> dict[str, Any]:
    """Apply all spec operations and commit exactly once."""
    if not isinstance(operations, list) or not operations:
        raise SpecError("ops must be a non-empty array")

    pending: dict[str, SpecItem] = {}
    written: list[SpecItem] = []
    written_relations: list[SpecRelation] = []
    written_anchors: list[SpecAnchor] = []
    try:
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise SpecError("each op must be an object")
            name = _operation_name(operation)
            if name == "create":
                fields = _item_payload(operation)
                if common_project_id and "project_id" not in fields:
                    fields["project_id"] = common_project_id
                values = _validate_item_fields(fields, partial=False)
                item_id = _clean_string(operation.get("id"), "id") or str(uuid.uuid4())
                if item_id in pending or _active_item(db, item_id) is not None:
                    raise SpecError(f"Spec item '{item_id}' already exists")
                values.setdefault("status", "draft")
                values.setdefault("confidence", "asserted")
                item = SpecItem(id=item_id, **values)
                db.add(item)
                pending[item_id] = item
                written.append(item)
            elif name == "update":
                item_id = _clean_string(operation.get("id"), "id", required=True)
                item = _resolve_item(db, item_id, pending)
                fields = _item_payload(operation)
                if common_project_id and "project_id" not in fields:
                    fields["project_id"] = common_project_id
                if not fields:
                    raise SpecError("update requires a patch")
                values = _validate_item_fields(fields, partial=True)
                for key, value in values.items():
                    setattr(item, key, value)
                if item not in written:
                    written.append(item)
            elif name == "supersede":
                old_id = _clean_string(
                    operation.get("old_id", operation.get("supersedes_id", operation.get("id"))),
                    "old_id", required=True,
                )
                old = _resolve_item(db, old_id, pending)
                fields = _item_payload(operation)
                fields.pop("supersedes_id", None)
                if common_project_id and "project_id" not in fields:
                    fields["project_id"] = common_project_id
                for key in ("project_id", "kind", "title", "body"):
                    fields.setdefault(key, getattr(old, key))
                values = _validate_item_fields(fields, partial=False)
                new_id = _clean_string(operation.get("new_id"), "new_id") or str(uuid.uuid4())
                if new_id in pending or _active_item(db, new_id) is not None:
                    raise SpecError(f"Spec item '{new_id}' already exists")
                values["supersedes_id"] = old.id
                values.setdefault("status", "active")
                values.setdefault("confidence", old.confidence or "asserted")
                new_item = SpecItem(id=new_id, **values)
                old.status = "superseded"
                db.add(new_item)
                pending[new_id] = new_item
                written.extend([old, new_item])
            elif name == "relation":
                from_id = _clean_string(operation.get("from_id", operation.get("from")), "from_id", required=True)
                to_id = _clean_string(operation.get("to_id", operation.get("to")), "to_id", required=True)
                relation_kind = _clean_string(
                    operation.get("relation", operation.get("kind")), "kind", required=True
                )
                if relation_kind not in RELATION_KINDS:
                    raise SpecError(f"relation kind must be one of {sorted(RELATION_KINDS)}")
                from_item = _resolve_item(db, from_id, pending)
                to_item = _resolve_item(db, to_id, pending)
                if from_item.project_id != to_item.project_id:
                    raise SpecError("relations must connect items in the same project")
                exists = db.query(SpecRelation).filter_by(
                    from_id=from_id, to_id=to_id, kind=relation_kind
                ).first()
                if exists is not None or any(
                    r.from_id == from_id and r.to_id == to_id and r.kind == relation_kind
                    for r in written_relations
                ):
                    continue
                relation = SpecRelation(from_id=from_id, to_id=to_id, kind=relation_kind)
                db.add(relation)
                written_relations.append(relation)
            elif name == "anchor":
                item_id = _clean_string(
                    operation.get("spec_item_id", operation.get("item_id", operation.get("id"))),
                    "spec_item_id", required=True,
                )
                item = _resolve_item(db, item_id, pending)
                repo = _clean_string(operation.get("repo"), "repo", required=True)
                path = _clean_string(operation.get("path"), "path", required=True)
                symbol = _clean_string(operation.get("symbol"), "symbol", required=True)
                anchor_relation = _clean_string(operation.get("relation"), "relation", required=True)
                if anchor_relation not in ANCHOR_RELATION_KINDS:
                    raise SpecError(f"anchor relation must be one of {sorted(ANCHOR_RELATION_KINDS)}")
                anchor_sha = _clean_string(operation.get("anchor_sha"), "anchor_sha")
                if not anchor_sha:
                    anchor_sha = compute_anchor_sha(repo, path, symbol)
                    if not anchor_sha:
                        raise SpecError(
                            f"could not resolve symbol '{symbol}' in {path} under {repo}; "
                            "pass anchor_sha explicitly if that path isn't checked out here"
                        )
                exists = db.query(SpecAnchor).filter_by(
                    spec_item_id=item.id, repo=repo, path=path, symbol=symbol, relation=anchor_relation
                ).first()
                if exists is not None or any(
                    a.spec_item_id == item.id and a.repo == repo and a.path == path
                    and a.symbol == symbol and a.relation == anchor_relation
                    for a in written_anchors
                ):
                    continue
                anchor = SpecAnchor(
                    spec_item_id=item.id, repo=repo, path=path, symbol=symbol,
                    relation=anchor_relation, anchor_sha=anchor_sha,
                )
                db.add(anchor)
                written_anchors.append(anchor)
            else:
                raise SpecError("op must be one of create, update, supersede, relation, anchor")

        # SessionLocal has autoflush=False; this is intentionally explicit.
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise

    unique_items = list(dict.fromkeys(written))
    return {
        "action": "spec_written", "count": len(operations),
        "items": [_item_snapshot(item) for item in unique_items],
        "relations": [_relation_snapshot(relation) for relation in written_relations],
        "anchors": [_anchor_snapshot(anchor) for anchor in written_anchors],
    }


def get_specs(
    db: Session,
    ids: list[str] | None = None,
    filters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one active spec cluster and all active relations touching it."""
    filters = dict(filters or {})
    unknown = set(filters) - _FILTER_FIELDS
    if unknown:
        raise SpecError(f"Unknown spec filter(s): {', '.join(sorted(unknown))}")
    if ids is not None:
        if not isinstance(ids, list) or not ids:
            raise SpecError("ids must be a non-empty array")
        ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))
        if not ids:
            raise SpecError("ids must contain at least one id")

    query = db.query(SpecItem).filter(SpecItem.archived_at.is_(None))
    if ids is not None:
        query = query.filter(SpecItem.id.in_(ids))
    for field, value in filters.items():
        column = getattr(SpecItem, field)
        if value is None:
            query = query.filter(column.is_(None))
        elif isinstance(value, (list, tuple, set)):
            query = query.filter(column.in_(list(value)))
        else:
            query = query.filter(column == value)
    items = query.order_by(SpecItem.project_id.asc(), SpecItem.id.asc()).limit(1000).all()
    seed_ids = [item.id for item in items]
    if not seed_ids:
        return {"action": "spec_fetched", "count": 0, "items": [], "relations": []}

    # An id query returns the connected active cluster, not just isolated
    # vertices.  This lets an executor consume a decision and its constraints
    # in one call without walking the graph item by item.  Filter queries stay
    # scoped to the requested filter and only include relations wholly inside
    # that result set.
    item_ids = list(seed_ids)
    all_relations: dict[tuple[str, str, str], SpecRelation] = {}
    expand_cluster = ids is not None
    while True:
        active_ids = select(SpecItem.id).where(SpecItem.archived_at.is_(None))
        relation_query = db.query(SpecRelation).filter(
            SpecRelation.from_id.in_(active_ids), SpecRelation.to_id.in_(active_ids),
        )
        if expand_cluster:
            relation_query = relation_query.filter(
                SpecRelation.from_id.in_(item_ids) | SpecRelation.to_id.in_(item_ids)
            )
        else:
            relation_query = relation_query.filter(
                SpecRelation.from_id.in_(item_ids), SpecRelation.to_id.in_(item_ids)
            )
        relations = relation_query.all()
        before = len(item_ids)
        for relation in relations:
            all_relations[(relation.from_id, relation.to_id, relation.kind)] = relation
            for related_id in (relation.from_id, relation.to_id):
                if related_id not in item_ids:
                    item_ids.append(related_id)
        if not expand_cluster or len(item_ids) == before:
            break

    if len(item_ids) != len(seed_ids):
        items = db.query(SpecItem).filter(
            SpecItem.id.in_(item_ids), SpecItem.archived_at.is_(None)
        ).order_by(SpecItem.project_id.asc(), SpecItem.id.asc()).all()
    relations = sorted(
        all_relations.values(),
        key=lambda relation: (relation.from_id, relation.to_id, relation.kind),
    )
    relation_data = [_relation_snapshot(relation) for relation in relations]
    by_item: dict[str, list[dict[str, str]]] = {item_id: [] for item_id in item_ids}
    for relation in relation_data:
        by_item[relation["from_id"]].append(relation)
        if relation["to_id"] != relation["from_id"]:
            by_item[relation["to_id"]].append(relation)
    return {
        "action": "spec_fetched", "count": len(items),
        "items": [_item_snapshot(item, by_item[item.id]) for item in items],
        "relations": relation_data,
    }


def get_stale_specs(db: Session, project_id: str) -> dict[str, Any]:
    """List active spec_item rows the commit-invalidation engine flagged stale.

    Read-only projection of state the invalidation engine already wrote
    (see `app.services.spec_anchor.apply_commit_staleness`) -- this never
    recomputes staleness itself, so it stays cheap and LLM-free.
    """
    project_id = _clean_string(project_id, "project", required=True)
    items = (
        db.query(SpecItem)
        .filter(
            SpecItem.project_id == project_id,
            SpecItem.status == "stale",
            SpecItem.archived_at.is_(None),
        )
        .order_by(SpecItem.updated_at.desc())
        .all()
    )
    return {
        "action": "spec_stale_fetched",
        "project_id": project_id,
        "count": len(items),
        "items": [
            {
                "id": item.id, "kind": item.kind, "title": item.title,
                "status": item.status, "reason": item.stale_reason,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in items
        ],
    }
