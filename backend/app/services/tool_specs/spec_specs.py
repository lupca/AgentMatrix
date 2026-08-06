from __future__ import annotations

from app.services.tool_specs.base import ToolSpec

SPEC_TOOL_SPECS: list[ToolSpec] = [
            ToolSpec(
                name="impl_design",
                description=(
                    "Use this to write down or read the single implementation "
                    "design that sits directly above a task -- files touched, "
                    "symbol changes, test plan, risks -- via action=create/get, "
                    "or to mechanically score it with action=score_completeness "
                    "(six fixed checks with reasons; never calls an LLM, never "
                    "scores by document length). This is not spec_write, which "
                    "records durable living-spec claims and code anchors across "
                    "the whole codebase, not a single task's design doc. No status "
                    "precondition beyond the task existing; action='get' on a task "
                    "with no design just returns empty -- call action='create' "
                    "first."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "get", "score_completeness"],
                            "default": "get",
                        },
                        "task_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "files": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "[{path, action: create|modify|delete, why}]",
                        },
                        "changes": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "[{symbol, signature, behavior, edge_cases}]",
                        },
                        "data_changes": {"type": "array"},
                        "test_plan": {"type": "array"},
                        "risks": {"type": "array"},
                        "non_goals": {"type": "array"},
                        "derived_from_sha": {"type": "string"},
                        "authored_by": {"type": "string"},
                        "reviewed_by": {"type": "string"},
                    },
                    "required": ["task_id"],
                },
                handler="impl_design",
                tier="deferred",
                permission="write",
                entity="impl_design",
                slash_alias=None,
                group="spec",
            ),
            ToolSpec(
                name="spec_write",
                description=(
                    "Use this to record or update a durable claim about how the "
                    "codebase actually behaves -- create/update/supersede a "
                    "spec_item, add a relation, anchor it to code, or manually "
                    "link it to a task (relations: implements, modifies, violates, "
                    "references) -- all as one transaction. This is not "
                    "impl_design, which is a single task's own upfront design doc, "
                    "not a durable cross-task claim about the codebase; also not "
                    "manage_notes, which is free-text agent notes rather than "
                    "anchored, provenance-tracked spec claims. For anchor "
                    "operations, omit anchor_sha and let the server compute it "
                    "(a manual 64-hex fallback is accepted only when the repo "
                    "isn't checked out). Always preserve derived_from_sha and "
                    "confidence when recording a claim. If an op is rejected for a "
                    "missing field (e.g. a task_link missing relation/confidence/"
                    "created_by), fix that op and resubmit -- spec_get shows what "
                    "already exists so you can check before you supersede it. "
                    "`realization` (agreed/built, whether the claim has become "
                    "code) is never a field you can set here -- any op carrying "
                    "it, top level or inside item/patch, is rejected outright. "
                    "It is derived read-only by spec_get from anchors and linked "
                    "task status; land code and anchor it instead of asserting it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "ops": {
                            "type": "array",
                            "description": (
                                "Batch of create, update, supersede, relation, anchor, or task_link "
                                "operations. A task_link requires spec_item_id, task_id, relation, "
                                "confidence, and created_by."
                            ),
                            "items": {"type": "object"},
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Optional project applied to create operations that omit project_id.",
                        },
                    },
                    "required": ["ops"],
                },
                handler="spec_write",
                tier="deferred",
                permission="write",
                entity="spec",
                slash_alias=None,
                group="spec",
                required_role="executor",
            ),
            ToolSpec(
                name="spec_get",
                description=(
                    "Use this before writing code, or before calling spec_write, to "
                    "read what's already claimed about the codebase -- by spec item "
                    "ids, a filter (project_id/kind/status/confidence/provenance), "
                    "or task_id for specs manually linked to a task. This is not "
                    "spec_stale, which lists items flagged stale by the commit "
                    "invalidation engine rather than returning the general active "
                    "set. Read-only, no status precondition. If ids/filter/task_id "
                    "match nothing, that's a valid empty result, not an error -- "
                    "check spec_stale if you expected something that used to exist. "
                    "Every returned item carries a server-derived `realization` "
                    "object ({state: agreed|built, why, next}) answering 'has this "
                    "actually become code', separate from `status` (which only "
                    "tracks whether the claim is still correct). Use "
                    "filter={'backlog': true} to see active items that are not yet "
                    "built -- what still needs to land -- or filter={'realization': "
                    "'built'|'agreed'} to select on that state directly."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Spec item ids to fetch as one cluster.",
                        },
                        "filter": {
                            "type": "object",
                            "description": (
                                "Filter by project_id, kind, status, confidence, or "
                                "provenance fields, plus two derived pseudo-fields: "
                                "backlog (bool, active items not yet built) and "
                                "realization ('agreed'|'built')."
                            ),
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Return spec items manually linked to this task.",
                        },
                    },
                    "required": [],
                },
                handler="spec_get",
                tier="deferred",
                permission="read",
                entity="spec",
                slash_alias=None,
                group="spec",
                required_role="executor",
                infer_task_scope=False,
            ),
            ToolSpec(
                name="spec_stale",
                description=(
                    "Use this to see which spec items for a project the commit-"
                    "triggered invalidation engine has already flagged as "
                    "possibly-outdated, and why (which symbol, which commit) -- "
                    "before trusting spec_get results or before deciding what to "
                    "supersede via spec_write. It's a pure lookup: it never "
                    "re-derives staleness itself and never asks an LLM whether an "
                    "item is still correct, unlike spec_write's supersede op, "
                    "which is the actual way to fix a flagged item. Precondition: "
                    "just needs a valid project id. If it returns nothing, the "
                    "project may have no stale items right now, or the project id "
                    "is wrong -- check with query_db (entity projects)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Project id to list stale spec items for.",
                        },
                    },
                    "required": ["project"],
                },
                handler="spec_stale",
                tier="deferred",
                permission="read",
                entity="spec",
                slash_alias=None,
                group="spec",
                required_role="executor",
            ),
]
