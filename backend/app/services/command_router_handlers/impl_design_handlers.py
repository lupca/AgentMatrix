import json
from collections.abc import Mapping

from app.services.impl_design import ImplDesignError, get_design, save_design, score_completeness


class ImplDesignHandlersMixin:
    async def _handle_impl_design(self, args: str, session_id: str) -> dict:
        del session_id
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {"error": "Invalid impl_design payload"}
        if not isinstance(payload, Mapping):
            return {"error": "impl_design payload must be an object"}

        action = str(payload.get("action") or "get").strip().lower()
        task_id = str(payload.get("task_id") or "").strip()
        if not task_id:
            return {"error": "task_id is required"}
        try:
            if action in {"create", "save", "write", "upsert"}:
                return {
                    "action": "impl_design_saved",
                    "design": await save_design(self.db, task_id, payload),
                }
            if action in {"get", "read"}:
                return {"action": "impl_design_read", "design": await get_design(self.db, task_id)}
            if action in {"score", "score_completeness", "check"}:
                result = await score_completeness(self.db, task_id)
                self.db.commit()
                return {"action": "impl_design_scored", "task_id": task_id, "completeness": result}
            return {"error": f"Unknown impl_design action: {action}"}
        except ImplDesignError as exc:
            return {"error": str(exc)}
