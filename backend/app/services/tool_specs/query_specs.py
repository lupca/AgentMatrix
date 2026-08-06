from __future__ import annotations

from app.services.tool_specs.base import DEFERRED_GROUPS, ToolSpec

QUERY_TOOL_SPECS: list[ToolSpec] = [
            ToolSpec(
                name="get_status",
                description=(
                    "Use this to check where a task currently sits in the "
                    "todo/dispatched/awaiting-review/in-review/done/failed lifecycle, "
                    "or to list recent tasks when you don't have a specific id yet. "
                    "It is a point-in-time snapshot, unlike wait_for_task, which "
                    "blocks until the task's state actually changes -- prefer "
                    "wait_for_task instead of calling get_status on a timer. No "
                    "precondition: it works on any task id at any status, including "
                    "one you're not sure exists. If the id is wrong it just returns "
                    "not-found; call it again with no task_id to browse recent tasks "
                    "and find the right one."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Task id, optional"},
                    },
                },
                handler="get_status",
                tier="eager",
                permission="read",
                entity="tasks",
                slash_alias="/status",
                group="query",
                required_role="executor",
            ),
            ToolSpec(
                name="get_run_output",
                description=(
                    "Use this when you have a specific run_id and want to read what "
                    "that agent run actually printed -- to check progress on a "
                    "running dispatch or inspect a finished one's output. It reads "
                    "persisted, replayable chunks, so it works the same whether the "
                    "run is mid-flight or long done -- unlike wait_for_task, which "
                    "blocks until the task's status changes rather than showing you "
                    "text. Precondition: you need both task_id and run_id, which "
                    "come from a prior dispatch_task/request_review result or "
                    "get_status. If run_id is wrong or unknown, call get_status on "
                    "the task first to find the current run."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Task scope."},
                        "run_id": {"type": "string", "description": "Agent run id."},
                        "offset": {"type": "integer", "default": 0},
                        "limit": {"type": "integer", "default": 20},
                    },
                    "required": ["task_id", "run_id"],
                },
                handler="get_run_output",
                tier="eager",
                permission="read",
                entity="agent_runs",
                slash_alias=None,
                group="query",
                required_role="executor",
            ),
            ToolSpec(
                name="get_stats",
                description=(
                    "Use this when you need aggregate numbers -- token usage, "
                    "authoritative API USD cost, run counts, plan-critic return "
                    "rate -- rather than one task's status; narrow with task_id or "
                    "agent_id, or omit both for a system-wide report. This is not "
                    "query_db: query_db runs arbitrary read-only SQL for questions "
                    "this canned report doesn't cover, while get_stats gives you the "
                    "specific cost/usage figures the budget brakes check (note CLI "
                    "subscription cost_usd is vendor telemetry, not an authoritative "
                    "charge -- use token totals for the CLI brake). No precondition "
                    "-- it's read-only against whatever history exists, including "
                    "none. If task_id or agent_id doesn't match anything, you just "
                    "get empty stats back; verify the id with get_status or query_db."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                    },
                },
                handler="get_stats",
                tier="eager",
                permission="read",
                entity="usage",
                slash_alias=None,
                group="query",
            ),
            ToolSpec(
                name="query_db",
                description=(
                    "Reach for this when the question is a genuine analytical query "
                    "over the data -- joins, filters, group-bys, counts across many "
                    "rows -- that none of the canned tools (get_status, get_stats, "
                    "get_task_events) answer directly. Only a single SELECT or WITH "
                    "statement is allowed; it is read-only and cannot be used to "
                    "mutate anything -- do NOT try to reach the DB any other way. "
                    "This is not get_stats: get_stats is a fixed cost/usage report, "
                    "query_db is free-form SQL for anything else. No status "
                    "precondition beyond a syntactically valid single-statement "
                    "SELECT/WITH. If the query is rejected (bad SQL, multiple "
                    "statements, unknown column), fix the SQL and call it again -- "
                    "check the schema summary below first.\n"
                    "Schema Summary:\n"
                    "- tasks (id, title, status [todo, dispatched, awaiting-review, in-review, done, cancelled, failed], project, executor, reviewer, priority, mode)\n"
                    "- inbox_items (id, content, project_id, task_id, tags, status, created_at)\n"
                    "- projects (id, name, status, repo_root, mode)\n"
                    "- agents (id, name, role, status, agent_type [cli, api], model) — role is legacy single value\n"
                    "- agents_view (id, name, role, roles[], capabilities_array[], status, model) — USE THIS for full roles/capabilities\n"
                    "- agent_roles (agent_id, role) — junction table for multi-role agents\n"
                    "- agent_capabilities (agent_id, capability) — junction table\n"
                    "- sessions (id, title, status, context_level, project_id, task_id)\n"
                    "- agent_runs (id, task_id, agent_id, kind [execute, review], status [queued, running, success, failed, cancelled], attempt)\n"
                    "- review_cycles (id, task_id, task_round_id, reviewer_id, reviewer_agent_run_id, status [requested, running, submitted, pass, changes, abandoned], verdict, source_gate_record_id, requested_at, submitted_at, completed_at) — one row per review pass over one task_round; the queryable home for verdicts (gate_records.input_payload used to be the only place this lived)\n"
                    "- review_findings (id, review_cycle_id, severity, title, detail, status [open, fixed, waived], waived_reason) — one row per reviewer finding; waived rows always carry waived_reason\n"
                    "- gate_records (id, task_id, gate_type, status, actor, mode, executor, reviewer, parent_id, created_at) — APPEND-ONLY: the original row keeps status='pending' FOREVER, the decision is a CHILD row carrying parent_id. Filtering on status='pending' therefore returns gates decided long ago (650 rows vs 8 truly open, measured). Same trap in admin_gate_records.\n"
                    "- open_gates (scope [task, admin], gate_record_id, task_id, gate_type, actor, mode, executor, reviewer, created_at, project, task_status, moot) — USE THIS to find gates that are genuinely undecided (pending with no decision child, task + admin gates together). gate_record_id is already in the form approve_gate takes ('admin:<id>' for admin gates); add WHERE NOT moot to drop gates on archived/done/cancelled tasks.\n"
                    "- knowledge_items (id, title, category, project, author, content)\n"
                    "- audit_log (id, task_id, action, actor, created_at)\n"
                    "- tool_metrics (id, tool, source, task_id, ok, cache_hit, duration_ms, result_count, bytes_out, error, payload JSON, created_at) — telemetry for graph/ocr/review tooling\n"
                    "- settings (key, value)\n\n"
                    "Examples:\n"
                    "SELECT project, count(*) FROM tasks WHERE status='dispatched' GROUP BY project\n"
                    "SELECT id, roles, capabilities_array FROM agents_view WHERE 'executor' = ANY(roles)\n"
                    "SELECT gate_record_id, task_id, gate_type, created_at FROM open_gates WHERE NOT moot ORDER BY created_at"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "sql": {
                            "type": "string",
                            "description": "A single raw SQL SELECT/WITH statement. Result is limited to 500 rows.",
                        },
                        "entity": {
                            "type": "string",
                            "description": "[DEPRECATED] Entity to query.",
                        },
                        "filters": {
                            "type": "object",
                            "description": "[DEPRECATED] Equality filters.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "[DEPRECATED] Max rows to return.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "[DEPRECATED] Row offset.",
                        },
                        "include_archived": {
                            "type": "boolean",
                            "description": "[DEPRECATED] Include archived rows.",
                        },
                    },
                    "required": [],
                },
                handler="query_db",
                tier="eager",
                permission="read",
                entity="query",
                slash_alias="/query",
                group="query",
            ),
            ToolSpec(
                name="get_task_events",
                description=(
                    "Use this when you want the event history/timeline for a task "
                    "-- what happened and in what order -- rather than just its "
                    "current status; pass since_id to fetch only events newer than "
                    "the last one you saw, or kind='decision' to filter to events "
                    "that need a coordinator decision. This is not wait_for_task: "
                    "wait_for_task blocks server-side until something new happens, "
                    "get_task_events is a non-blocking poll you drive yourself. No "
                    "status precondition -- works for a task in any state, or omit "
                    "task_id for events across all tasks. If since_id is stale or "
                    "invalid, drop it and call again from the start to resync."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "Filter to one task; omit for all tasks."},
                        "since_id": {"type": "integer", "description": "Return only events with id greater than this."},
                        "kind": {"type": "string", "enum": ["decision", "info"]},
                        "event_types": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "default": 50},
                    },
                    "required": [],
                },
                handler="get_task_events",
                tier="deferred",
                permission="read",
                entity="tasks",
                slash_alias="/events",
                group="query",
                required_role="executor",
            ),
            ToolSpec(
                name="wait_for_task",
                description=(
                    "Use this right after dispatching or requesting review on a "
                    "task, when you just need to know the moment something "
                    "happens -- a status change, a pending gate needing approval, "
                    "or a terminal state (done/failed/cancelled) -- instead of "
                    "checking repeatedly. It blocks server-side up to "
                    "timeout_seconds and returns the task snapshot, latest run, and "
                    "new events in one call; this is not get_status, which returns "
                    "immediately with only a snapshot and no blocking. No status "
                    "precondition -- works on a task in any state. On timeout "
                    "(changed=false) there's nothing to recover: just call it again "
                    "with the returned cursor as since_event_id to keep waiting."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "since_event_id": {
                            "type": "integer",
                            "description": (
                                "Event cursor from the previous call; returns any event newer "
                                "than this. Omit it to start watching after the latest event "
                                "that already exists when the call begins."
                            ),
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "default": 55,
                            "description": "How long to block server-side (5-120). Keep below your client's tool timeout.",
                        },
                    },
                    "required": ["task_id"],
                },
                handler="wait_for_task",
                tier="deferred",
                permission="read",
                entity="tasks",
                slash_alias=None,
                group="query",
                required_role="executor",
            ),
            ToolSpec(
                name="suggest_agents",
                description=(
                    "Use this when you want to see which agent dispatch_task would "
                    "pick, or compare candidates, before actually committing to a "
                    "dispatch -- it returns the top candidates with score and "
                    "reason from the same matcher (skill/performance/load/cost/"
                    "risk), and is purely read-only, unlike dispatch_task which "
                    "actually assigns the executor and moves the task. No status "
                    "precondition -- works on any task id you want a ranking for. "
                    "If the ranking looks wrong or empty, check the task's project "
                    "and required capabilities with get_status/query_db rather than "
                    "retrying suggest_agents itself."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["executor", "reviewer", "coordinator", "spec_plan"],
                            "default": "executor",
                        },
                        "top_n": {"type": "integer", "default": 3},
                    },
                    "required": ["task_id"],
                },
                handler="suggest_agents",
                tier="deferred",
                permission="read",
                entity="agents",
                slash_alias=None,
                group="query",
            ),
]
