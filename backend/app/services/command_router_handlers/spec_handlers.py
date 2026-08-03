import json
from collections.abc import Mapping

from app.services.spec_service import SpecError, get_specs, write_specs


class SpecHandlersMixin:
    async def _handle_spec_write(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {"error": "Invalid spec_write payload"}
        if not isinstance(payload, Mapping):
            return {"error": "spec_write payload must be an object"}
        try:
            return write_specs(
                self.db, payload.get("ops"),
                common_project_id=str(payload["project_id"]).strip() if payload.get("project_id") else None,
            )
        except SpecError as exc:
            return {"error": str(exc)}

    async def _handle_spec_get(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {"error": "Invalid spec_get payload"}
        if not isinstance(payload, Mapping):
            return {"error": "spec_get payload must be an object"}
        ids = payload.get("ids")
        filters = payload.get("filter", payload.get("filters"))
        if filters is not None and not isinstance(filters, Mapping):
            return {"error": "filter must be an object"}
        if filters is None:
            filters = {key: payload[key] for key in (
                "project_id", "kind", "status", "confidence", "derived_by",
                "source_doc_id", "supersedes_id",
            ) if key in payload}
        try:
            return get_specs(self.db, ids=ids, filters=filters)
        except SpecError as exc:
            return {"error": str(exc)}
