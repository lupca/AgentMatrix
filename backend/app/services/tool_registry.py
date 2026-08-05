"""Single source of truth for every coordinator tool (ADR-001 §D1).

Each tool is declared exactly once as a :class:`ToolSpec`. Everything else —
the OpenAI-format schema list (``get_tool_definitions``), the slash-command
table (``CommandRouter``), and the ``GET /api/tools`` dump — is a projection
over ``TOOL_REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Tier = Literal["eager", "deferred"]
Permission = Literal["read", "write", "admin"]
Role = Literal["coordinator", "executor"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    """Canonical tool name, e.g. ``create_task``."""

    description: str
    parameters: dict[str, Any]
    """JSON Schema for the tool's arguments (OpenAI function format)."""

    handler: str
    """Command identifier ``CommandRouter.execute`` dispatches to (``_handle_<handler>``)."""

    tier: Tier
    permission: Permission
    entity: str
    slash_alias: str | None
    group: str
    required_role: Role = "coordinator"
    infer_task_scope: bool = True
    """Whether an executor token fills an omitted optional ``task_id``."""


# Deferred-tool groups loadable via the ``load_tools`` meta-tool (ADR-001
# §D3).
DEFERRED_GROUPS: tuple[str, ...] = (
    "task_lifecycle",
    "admin",
    "session",
    "research",
    "query",
    "spec",
)


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            name="create_task",
            description=(
                "Reach for this when the user describes work that does not "
                "correspond to any existing task row yet -- a new feature, bug, "
                "or chore to track. This is the only tool that mints a task id; "
                "it is not update_task, which edits an id that already exists "
                "and errors if you pass one that doesn't. No status precondition: "
                "it always starts a task at 'todo'. If the create call is "
                "rejected for a missing title, just retry with one; there is no "
                "separate recovery tool needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "project": {"type": "string", "description": "Project id"},
                    "description": {
                        "type": "string",
                        "description": (
                            "Full task specification, stored as raw_input — the "
                            "field the planner reads. Include the problem, the "
                            "evidence, the constraints and what must NOT be done. "
                            "A task created without it has only a title to plan "
                            "from and will be refused at dispatch."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Task ids that must reach 'done' before this task "
                            "may dispatch"
                        ),
                    },
                },
                "required": ["title"],
            },
            handler="create_task",
            tier="eager",
            permission="write",
            entity="tasks",
            slash_alias="/pm",
            group="task_lifecycle",
        ),
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
            name="manage_inbox",
            description=(
                "Use this when someone drops a raw idea, note, or maybe-later "
                "item that isn't ready to be a task yet -- add/update/delete/list "
                "it as free text, with no gate to approve. This is not "
                "create_task: create_task immediately starts a real, dispatchable "
                "task row; manage_inbox just parks the idea until you call it "
                "with action='promote' to turn it into one. No status "
                "precondition -- inbox items don't have a task lifecycle. If an "
                "action is rejected for a missing id (update/delete/promote on an "
                "item that doesn't exist), call it again with action='list' to "
                "find the right id first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update", "delete", "list", "promote"]},
                    "id": {"type": "string"}, "content": {"type": "string"},
                    "project_id": {"type": ["string", "null"]}, "task_id": {"type": ["string", "null"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": ["open", "triaged", "dropped"]},
                    "q": {"type": "string"}, "title": {"type": "string"}, "patch": {"type": "object"},
                },
                "required": ["action"],
            },
            handler="manage_inbox", tier="eager", permission="write", entity="inbox_items",
            slash_alias=None, group="task_lifecycle",
        ),
        ToolSpec(
            name="ask_human",
            description=(
                "Use this when you need a HUMAN, specifically, to decide "
                "something -- an irreversible choice, a design tradeoff, "
                "spending real money, anything outside your authority to "
                "decide alone. This is not manage_inbox: manage_inbox parks a "
                "note for later with no gate and no reply expected; "
                "ask_human actively notifies a human and expects one. This is "
                "ONE-WAY: it queues a Telegram message "
                "and returns immediately. There is no get_answer, no "
                "wait_for_human, and none will ever be added -- do not poll "
                "or wait in a loop after calling this. The human answers by "
                "typing into the coordinator chat session directly, a path "
                "that does not go through any tool call at all; your job "
                "after calling ask_human is to stop and let the turn end, "
                "not to wait for a return value that will never come. "
                "why_human is mandatory and must explain why a human, not a "
                "machine, has to answer -- an empty or missing why_human "
                "means this is machine escalation dressed up as a question, "
                "and the call is rejected. task_id is optional: pass it when "
                "the question is about one specific task (the task is then "
                "labeled as waiting on a human, not stuck on a machine); "
                "omit it for a question with no single task attached."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question for the human, in full."},
                    "why_human": {
                        "type": "string",
                        "description": (
                            "Required, non-empty. Why only a human can answer this "
                            "-- not a restatement of the question."
                        ),
                    },
                    "task_id": {"type": ["string", "null"], "description": "Task this question is about, if any."},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional short list of choices, if the question is multiple-choice.",
                    },
                },
                "required": ["question", "why_human"],
            },
            handler="ask_human", tier="deferred", permission="write", entity="task_events",
            slash_alias=None, group="task_lifecycle", required_role="executor",
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
                "- knowledge_items (id, title, category, project, author, content)\n"
                "- audit_log (id, task_id, action, actor, created_at)\n"
                "- tool_metrics (id, tool, source, task_id, ok, cache_hit, duration_ms, result_count, bytes_out, error, payload JSON, created_at) — telemetry for graph/ocr/review tooling\n"
                "- settings (key, value)\n\n"
                "Examples:\n"
                "SELECT project, count(*) FROM tasks WHERE status='dispatched' GROUP BY project\n"
                "SELECT id, roles, capabilities_array FROM agents_view WHERE 'executor' = ANY(roles)"
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
            name="dispatch_task",
            description=(
                "Use this once a task is ready to actually be worked -- it "
                "assigns an executor agent and moves the task from 'todo' to "
                "'dispatched', kicking off the run. Precondition: the task must "
                "be in 'todo' (and, per the plan-only autonomy mode, may need a "
                "spec/plan already generated -- see generate_spec_plan). This is "
                "not request_review, which dispatches a reviewer for a task "
                "that's already awaiting-review; dispatch_task is for the first, "
                "executor leg of the work. If dispatch is rejected because the "
                "task isn't in 'todo' -- e.g. it's 'failed' -- call reopen_task "
                "first to get it back to a dispatchable state."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "executor": {"type": "string"},
                    "effort": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "extra-high", "max"],
                        "description": (
                            "Override the executor agent's default effort for "
                            "this dispatch only."
                        ),
                    },
                },
                "required": ["task_id"],
            },
            handler="dispatch_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/dispatch",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="record_verdict",
            description=(
                "Use this once you (the reviewer) have actually examined a "
                "task's diff against its acceptance criteria and reached a "
                "pass/changes decision -- it records that verdict as a gate "
                "pending approval. Precondition: the task must be 'in-review' "
                "(i.e. request_review has already dispatched a reviewer run for "
                "it). This is not approve_gate: record_verdict is the reviewer "
                "stating the decision for the first time, approve_gate is "
                "confirming a pending gate (verdict or otherwise) that already "
                "exists. Never record a verdict for a review you did not "
                "actually run. If this is rejected because the task isn't "
                "'in-review' yet, call request_review first to dispatch the "
                "review run."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "changes"]},
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["task_id", "verdict"],
            },
            handler="verdict",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/verdict",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="attach_result",
            description=(
                "Use this when an executor already made a commit but the task "
                "record's result_ref never got set -- e.g. an agent run reported "
                "success without printing RESULT_REF, or someone committed by "
                "hand outside a dispatch. It attaches the commit and always "
                "moves the task to awaiting-review so an independent reviewer "
                "can verify it; it can never mark a task done itself. "
                "Precondition: the task must be 'dispatched' (a retry after the "
                "first successful call is also accepted). This is not "
                "land_task: land_task performs the actual merge of an already "
                "reviewed, pass-verdict result into the integration branch; "
                "attach_result only records which commit to review and never "
                "merges anything. If the task is 'failed' instead of "
                "'dispatched', call reopen_task first to bring it back to a "
                "state where attach_result is valid. If the work was done "
                "OUTSIDE this system -- by you, by your own subagents, by hand "
                "-- pass external_executor and attach straight from 'todo': "
                "never dispatch an agent just to redo finished work so the "
                "record will accept it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id"},
                    "commit": {
                        "type": "string",
                        "description": "Git commit hash or reference to attach",
                    },
                    "external_executor": {
                        "type": "string",
                        "description": (
                            "Who actually did the work, when no AgentRun from "
                            "this system produced it (e.g. '@coordinator' or a "
                            "subagent name). Lets a task be attached from "
                            "'todo'/'changes-requested' instead of forcing a "
                            "throwaway dispatch. Recorded as the task's "
                            "executor, so four-eyes still applies: the reviewer "
                            "must be someone else. The event records provenance "
                            "as 'external' -- do not use it to disguise work "
                            "that an agent run actually did."
                        ),
                    },
                    "option": {
                        "type": "string",
                        "enum": ["request_review"],
                        "default": "request_review",
                        "description": (
                            "Kept as an explicit field only so a caller can see, "
                            "in the schema itself, that attaching a result always "
                            "routes to review and never to done -- there is no "
                            "other value to choose; omit it and the same "
                            "'request_review' behavior applies automatically."
                        ),
                    },
                },
                "required": ["task_id", "commit"],
            },
            handler="attach_result",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/attach-result",
            group="task_lifecycle",
            required_role="executor",
        ),
        ToolSpec(
            name="approve_gate",
            description=(
                "Use this once a pending gate (dispatch, review_order, verdict, "
                "escalation, safety_brake, or an admin gate from manage_project/"
                "manage_agent/manage_knowledge/update_settings) has actually been "
                "checked and you're ready to let it proceed or reject it. Pass "
                "gate_record_id when you have it (use the 'admin:<id>' form "
                "returned by manage_* tools for admin gates); task_id alone "
                "resolves that task's pending gate. This is not record_verdict: "
                "record_verdict is the reviewer originating a pass/changes "
                "decision, approve_gate is confirming a gate that already exists "
                "and is pending -- including the verdict gate record_verdict "
                "just created. Precondition: a gate must actually be pending "
                "(get_status or a pending_approvals note tells you this). If "
                "there's no pending gate to approve, there is nothing to recover "
                "-- the underlying action (dispatch_task, request_review, "
                "record_verdict, manage_*) has to be called first to create one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "gate_record_id": {
                        "type": "string",
                        "description": (
                            "Gate record id, or 'admin:<id>' for an admin "
                            "gate pending from manage_project/manage_agent/"
                            "manage_knowledge/update_settings."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task whose pending gate to approve (fallback when gate_record_id is unknown).",
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["approved", "rejected"],
                        "description": "Human decision for the gate; defaults to approved.",
                    },
                    "evidence": {
                        "type": "array",
                        "description": (
                            "Required to approve a verdict gate: the checks you "
                            "actually ran. Each item is {check, result} -- check "
                            "is the command you ran, result is its real output. "
                            "Stored on the ledger row of this decision."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "check": {"type": "string"},
                                "result": {"type": "string"},
                            },
                            "required": ["check", "result"],
                        },
                    },
                },
                "required": [],
            },
            handler="approve_gate",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/approve",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="land_task",
            description=(
                "Use this to actually merge a task's reviewed result into the "
                "project's integration branch -- normally this happens "
                "automatically the moment the verdict gate is approved, so you "
                "only need to call it by hand to retry after a reported landing "
                "failure, or to backfill a legacy 'done' task whose ct-run "
                "branch was never merged. Precondition: the task needs an "
                "approved pass verdict on record (require_approved_pass_verdict) "
                "and a result_ref; this is not attach_result or record_verdict, "
                "neither of which ever performs the merge -- land_task is the "
                "only tool that touches the integration branch. Never merge "
                "ct-run/* branches yourself outside this tool. If land_task "
                "reports 'landing_failed', fix whatever the error names in the "
                "repo and call land_task again; if there's no approved pass "
                "verdict yet, get the review through record_verdict and "
                "approve_gate first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id"},
                },
                "required": ["task_id"],
            },
            handler="land_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias=None,
            group="task_lifecycle",
        ),
        ToolSpec(
            name="cancel_task",
            description=(
                "Use this when a task is no longer wanted at all, regardless of "
                "its current status, and should stop moving through the "
                "lifecycle for good. This is not archive_task: archive_task "
                "soft-deletes a task (usually already done or otherwise "
                "finished) while preserving it for history and lets you restore "
                "it; cancel_task ends an active task's workflow. No status "
                "precondition -- it works from most in-flight states. If you "
                "cancel a task by mistake, there is no direct undo tool; use "
                "update_task to review its state or create_task to start the "
                "work again."
            ),
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            handler="cancel_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/cancel",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="reopen_task",
            description=(
                "Use this when get_status shows a task stuck at 'failed' for a "
                "reason unrelated to the work itself -- a budget brake firing "
                "after the result was already delivered, or an escalation "
                "raised while a step was still in flight -- and you want it "
                "workable again. A task with a delivered result_ref returns to "
                "awaiting-review (an independent reviewer still has to pass it "
                "before landing, via request_review/record_verdict); one "
                "without returns to 'todo' so dispatch_task can pick it up "
                "again. Precondition: the task must be 'failed' -- this is not "
                "cancel_task, which ends a task for good with no path back; "
                "reopen_task exists specifically to undo a 'failed' state. If "
                "reopen_task itself is rejected because the task isn't "
                "'failed', there's nothing to recover -- check get_status for "
                "its real status and use the tool matching that state instead."
            ),
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            handler="reopen_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/reopen",
            group="task_lifecycle",
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
            name="archive_task",
            description=(
                "Use this to tidy up a task that's finished or no longer "
                "relevant to active views, without losing its history -- pass "
                "restore=true to bring a previously archived task back. This is "
                "not cancel_task: cancel_task stops an active task's workflow "
                "outright, archive_task hides an already-settled task and can "
                "be undone. No status precondition -- any task can be archived. "
                "If you archived the wrong task, there's no separate recovery "
                "tool: call archive_task again on the same id with restore=true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "restore": {"type": "boolean", "default": False},
                },
                "required": ["task_id"],
            },
            handler="archive_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias=None,
            group="task_lifecycle",
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
        ToolSpec(
            name="request_review",
            description=(
                "Use this once a task has a result attached (via attach_result "
                "or a successful execute run) and is sitting at 'awaiting-"
                "review' -- it dispatches a real /code-review run against the "
                "committed base..head range and moves the task to 'in-review'. "
                "If reviewer is omitted, one is auto-selected and is always "
                "independent from the executor (four-eyes); if no independent "
                "reviewer is available this fails rather than lowering the bar, "
                "and an explicitly requested invalid reviewer is rejected with "
                "valid alternatives rather than silently replaced. This is not "
                "dispatch_task, which assigns the first, executor leg of work "
                "on a 'todo' task -- request_review is the second, reviewer "
                "leg on a task that already has a result. If it's rejected "
                "because the task isn't 'awaiting-review' yet, attach the "
                "result first (attach_result) or wait for the execute run to "
                "finish."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "reviewer": {
                        "type": "string",
                        "description": "Agent id to review; auto-selected if omitted.",
                    },
                },
                "required": ["task_id"],
            },
            handler="request_review",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/request-review",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="generate_spec_plan",
            description=(
                "Use this before dispatching a non-trivial 'todo' task when you "
                "want a researched spec/plan in place first, and no plan exists "
                "on the task yet -- it runs a CLI agent inside the project repo "
                "to write the plan, then an independent, focused plan critic "
                "(150k token budget, no diff access, may reject only with "
                "reproducible evidence) before the task is dispatch-eligible. "
                "This is not critique_spec_plan: critique_spec_plan re-runs "
                "only the critic against a plan that's already written, never "
                "calling the planner again. Precondition: task should be "
                "'todo' with no usable plan yet (plan-only autonomy mode "
                "requires this gate before dispatch_task will accept it). If "
                "the critic step fails after the plan itself was written "
                "successfully, don't call generate_spec_plan again and burn "
                "another planner run -- call critique_spec_plan instead, since "
                "the plan already persisted on the task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Agent to generate the spec/plan; auto-suggested "
                            "if omitted."
                        ),
                    },
                    "critic_id": {
                        "type": "string",
                        "description": (
                            "Independent CLI agent to criticize the plan; auto-suggested "
                            "if omitted. Requires agent_id when explicitly provided."
                        ),
                    },
                },
                "required": ["task_id"],
            },
            handler="generate_spec_plan",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/spec-plan",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="critique_spec_plan",
            description=(
                "Use this when a task already has a plan stored on it (from a "
                "prior generate_spec_plan call) and the critic step needs to "
                "run or re-run -- because it failed, was rejected, or you want "
                "a fresh independent pass -- without paying for another planner "
                "call. It never calls the planner itself, only the critic, and "
                "each run appends a new plan_critic gate record. This is not "
                "generate_spec_plan, which is required first to actually write "
                "the plan when none exists yet. Precondition: the task must "
                "already have a stored plan. If critique_spec_plan is rejected "
                "because there's no plan to critique, call generate_spec_plan "
                "first to produce one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "critic_id": {
                        "type": "string",
                        "description": (
                            "Independent CLI agent to criticize the plan; auto-suggested "
                            "if omitted."
                        ),
                    },
                },
                "required": ["task_id"],
            },
            handler="critique_spec_plan",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/critique-plan",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="compact_context",
            description=(
                "Use this on your own session when it has grown long and older "
                "messages are eating context budget you'd rather spend on the "
                "current work -- it summarizes older session messages in place. "
                "This is not save_project_context, which persists durable "
                "project conventions for future sessions to read; "
                "compact_context only shrinks this session's own history. No "
                "precondition beyond having an active session. There's no "
                "specific rejection path -- if it doesn't help enough, keep "
                "working and call it again later as the session grows further."
            ),
            parameters={"type": "object", "properties": {}},
            handler="compact_context",
            tier="deferred",
            permission="write",
            entity="sessions",
            slash_alias="/compact",
            group="session",
        ),
        ToolSpec(
            name="manage_project",
            description=(
                "Use this when the unit you're creating/changing is the "
                "project itself -- its repo_root, name, mode, status -- rather "
                "than a task inside it; action=create/update/archive/restore, "
                "with no hard delete. This is not update_task, which edits a "
                "task's own fields and never touches project-level settings. "
                "Admin-permission: in supervised mode this creates a pending "
                "gate awaiting approve_gate rather than applying immediately; "
                "in bypass mode it applies right away. If the call returns a "
                "pending admin gate, call approve_gate with the 'admin:<id>' "
                "form to let it proceed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive", "restore"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Project id (required for update/archive).",
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "context_md": {"type": "string"},
                    "status": {"type": "string"},
                    "repo_root": {"type": "string"},
                    "task_prefix": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["supervised", "bypass"],
                        "default": "supervised",
                        "description": "Gate mode for this mutation.",
                    },
                },
                "required": ["action"],
            },
            handler="manage_project",
            tier="deferred",
            permission="admin",
            entity="projects",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="manage_agent",
            description=(
                "Use this to register a new CLI/API agent, change one's "
                "roles/capabilities/model/effort, or disable/archive/restore "
                "one -- action=create/update/disable/archive/restore. api_key "
                "is write-only: it is encrypted before any record is "
                "persisted, never echoed back, and never readable through any "
                "tool -- check has_api_key instead of expecting api_key back. "
                "This is not suggest_agents, which only ranks existing agents "
                "for a task and never mutates the roster. Admin-permission: in "
                "supervised mode this creates a pending gate awaiting "
                "approve_gate rather than applying immediately; in bypass mode "
                "it applies right away. If it returns a pending admin gate, "
                "call approve_gate with the 'admin:<id>' form."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "disable", "archive", "restore"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Agent id (required for update/disable).",
                    },
                    "name": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["executor", "reviewer", "coordinator", "spec_plan"],
                        "description": "Primary role (legacy). Prefer 'roles' for multi-role agents.",
                    },
                    "roles": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["executor", "reviewer", "coordinator", "spec_plan"],
                        },
                        "description": "Agent roles. Most agents have [executor, reviewer].",
                    },
                    "status": {"type": "string"},
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent capabilities (code, backend, review, architecture, etc.)",
                    },
                    "model": {"type": "string"},
                    "effort": {"type": "string"},
                    "cli": {"type": "string"},
                    "agent_type": {"type": "string", "enum": ["cli", "api"]},
                    "provider": {
                        "type": "string",
                        "enum": ["anthropic", "google", "openai"],
                    },
                    "base_url": {"type": "string"},
                    "api_key": {
                        "type": "string",
                        "description": (
                            "API credential for agent_type=api. Write-only: "
                            "encrypted at rest, redacted from gate/audit "
                            "records, never returned."
                        ),
                    },
                    "is_default": {"type": "boolean"},
                    "mode": {
                        "type": "string",
                        "enum": ["supervised", "bypass"],
                        "default": "supervised",
                        "description": "Gate mode for this mutation.",
                    },
                },
                "required": ["action"],
            },
            handler="manage_agent",
            tier="deferred",
            permission="admin",
            entity="agents",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="manage_knowledge",
            description=(
                "Use this to save or edit a standalone knowledge-base article -- "
                "action=create/update/archive/restore, no hard delete. This is "
                "not manage_notes: manage_notes handles smaller agent notes with "
                "many-to-many links to specific projects/tasks and semantic "
                "search; manage_knowledge is for titled, categorized reference "
                "content with no gate to approve. No status precondition beyond "
                "an id existing for update/archive/restore. If update/archive "
                "is rejected for an unknown id, list existing items via "
                "query_db (entity knowledge_items) to find the right one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive", "restore"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Knowledge item id (required for update/archive).",
                    },
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "project": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["action"],
            },
            handler="manage_knowledge",
            tier="deferred",
            permission="write",
            entity="knowledge",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="manage_notes",
            description=(
                "Use this for a smaller, linkable note tied to one or more "
                "specific projects/tasks -- a fact, decision, observation, "
                "procedure, or preference you want future sessions to find via "
                "semantic search. This is not manage_knowledge: manage_knowledge "
                "is for standalone titled/categorized reference articles; "
                "manage_notes is for notes with explicit project_id/task_id "
                "links and a query='...' semantic search action. No status "
                "precondition for save/search/list; link/archive need an "
                "existing note id, from a prior save or a search/list call. If "
                "link/archive is rejected for an unknown id, call action='list' "
                "or action='search' first to find the right id.\n"
                "- save: create note, pass project_id/task_id to link immediately\n"
                "- search: semantic search (query auto-embedded) or filter by project_id/task_id\n"
                "- link: link existing note to additional projects/tasks\n"
                "- list: list notes, filter by project_id/task_id/note_type\n"
                "- archive: soft-delete note\n"
                "Notes can be linked to MULTIPLE projects and tasks simultaneously."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["save", "search", "link", "list", "archive"]},
                    "id": {"type": "string", "description": "Note id (required for link/archive)."},
                    "title": {"type": "string", "description": "Note title (required for save)."},
                    "content": {"type": "string", "description": "Note content (required for save)."},
                    "note_type": {"type": "string", "enum": ["fact", "decision", "observation", "procedure", "preference"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "embedding": {"type": "array", "items": {"type": "number"}, "description": "1536-dim vector for search (auto-generated if query provided)."},
                    "project_id": {"type": "string", "description": "Link note to this project (save/link) or filter by project (list/search)."},
                    "task_id": {"type": "string", "description": "Link note to this task (save/link) or filter by task (list/search)."},
                    "query": {"type": "string", "description": "Semantic search query (auto-embedded)."},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["action"],
            },
            handler="manage_notes",
            tier="deferred",
            permission="write",
            entity="agent_notes",
            slash_alias=None,
            group="research",
        ),
        ToolSpec(
            name="update_settings",
            description=(
                "Use this to change a system-wide setting like autonomy mode -- "
                "not a specific project's or agent's own fields (use "
                "manage_project/manage_agent for those). Reads the whitelist "
                "via query_db (SELECT key, value FROM settings) to see what's "
                "writable; keys outside the whitelist are rejected. "
                "Admin-permission: in supervised mode this creates a pending "
                "gate awaiting approve_gate rather than applying immediately; "
                "in bypass mode it applies right away -- if it returns a "
                "pending admin gate, call approve_gate with the 'admin:<id>' "
                "form. "
                "The `autonomy` setting controls task mode behavior: "
                "`supervised` requires human approval at every gate, "
                "`auto` allows low-risk tasks to bypass gates automatically, "
                "and `plan-only` blocks dispatch entirely. "
                "The `default_mode` key is not a writable setting and will "
                "be rejected."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Setting key; must be on the whitelist.",
                    },
                    "value": {"description": "New value for the setting (any JSON type)."},
                    "mode": {
                        "type": "string",
                        "enum": ["supervised", "bypass"],
                        "default": "supervised",
                        "description": "Gate mode for this mutation.",
                    },
                },
                "required": ["key", "value"],
            },
            handler="update_settings",
            tier="deferred",
            permission="admin",
            entity="settings",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="update_task",
            description=(
                "Use this to correct or extend a task's own content -- "
                "raw_input, plan, coordinator_notes, acceptance_criteria, "
                "priority, tags, or dependencies -- while it sits at whatever "
                "status it's already at. This is not manage_project, which "
                "edits the project the task lives in, not the task itself; "
                "also, update_task never changes task status -- use "
                "dispatch_task, record_verdict, or approve_gate for status "
                "transitions. No status precondition, but the task id must "
                "exist. If the patch is rejected (e.g. a dependency cycle), "
                "fix the patch content and call update_task again -- there's "
                "no separate recovery tool. Prefer coordinator_notes over "
                "plan for a coordinator reply/decision meant for the planner "
                "to read: plan is planner OUTPUT and generate_spec_plan's "
                "write_spec_plan overwrites it wholesale on the next run, "
                "silently discarding anything written there in the meantime; "
                "coordinator_notes is coordinator-owned and the planner only "
                "ever reads it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "patch": {
                        "type": "object",
                        "description": (
                            "Fields to update: raw_input (replace semantics), plan, "
                            "coordinator_notes, acceptance_criteria, "
                            "priority, tags. Dependency edits: "
                            "add_depends_on / remove_depends_on (arrays of "
                            "task ids; cycles are rejected)."
                        ),
                    },
                },
                "required": ["task_id", "patch"],
            },
            handler="update_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias=None,
            group="task_lifecycle",
        ),
        ToolSpec(
            name="get_minimal_context",
            description=(
                "Use this when you're about to touch code and want a compact, "
                "relevant slice of the project's code graph for a natural-"
                "language query, instead of reading whole files blind. This is "
                "not get_impact_radius: get_impact_radius answers 'what breaks "
                "if I change this file', get_minimal_context answers 'what code "
                "is relevant to this topic'. Read-only, no status precondition "
                "-- works for any project the graph has indexed. If results "
                "look thin or empty, the graph may not be built for this repo "
                "yet; save_project_context after scanning the repo, or narrow "
                "the query and try again."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Code or task context to find."},
                    "limit": {"type": "integer", "default": 10, "description": "Maximum matching nodes."},
                },
                "required": ["query"],
            },
            handler="get_minimal_context",
            tier="deferred",
            permission="read",
            entity="research",
            slash_alias=None,
            group="research",
            required_role="executor",
        ),
        ToolSpec(
            name="get_impact_radius",
            description=(
                "Use this before or after editing a specific file, when you "
                "need to know what else could be affected -- it returns a "
                "compact blast-radius summary with risk and affected-file "
                "count for that one file. This is not get_minimal_context, "
                "which searches by topic/query across the whole graph rather "
                "than tracing dependents of one known file path. Read-only, no "
                "status precondition. If the file path isn't recognized, "
                "double-check it's project-relative (not absolute) and that "
                "the project's code graph has been built."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Project-relative file path."},
                    "max_depth": {
                        "type": "integer",
                        "default": 2,
                        "minimum": 1,
                        "maximum": 10,
                        "description": "Maximum dependency traversal depth.",
                    },
                },
                "required": ["file"],
            },
            handler="get_impact_radius",
            tier="deferred",
            permission="read",
            entity="research",
            slash_alias=None,
            group="research",
            required_role="executor",
        ),
        ToolSpec(
            name="save_project_context",
            description=(
                "Use this after scanning a repo's conventions and boundaries, "
                "as an executor, to persist that project context (context_md, "
                "up to 5 scoped rules) so it gets injected into future dispatch "
                "and review prompts automatically. This is not compact_context, "
                "which shrinks this session's own message history and has "
                "nothing to do with project conventions. Precondition: needs "
                "task_id (so an executor token passes the task-scope check) and "
                "project_id. If it's rejected for a missing task_id/project_id, "
                "supply them from the current dispatch's task/project rather "
                "than retrying blind."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Task id this context-generation run is scoped to "
                            "(required for executor tokens to pass task-scope check)"
                        ),
                    },
                    "project_id": {"type": "string", "description": "Project id"},
                    "context_md": {
                        "type": "string",
                        "description": "Markdown project context, max 150 lines",
                    },
                    "rules": {
                        "type": "array",
                        "description": "Up to 5 scoped rules",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "globs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "content": {"type": "string"},
                            },
                            "required": ["name", "content"],
                        },
                    },
                },
                "required": ["task_id", "project_id", "context_md"],
            },
            handler="save_project_context",
            tier="deferred",
            permission="write",
            entity="projects",
            slash_alias=None,
            group="task_lifecycle",
            required_role="executor",
        ),
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
        ToolSpec(
            name="load_tools",
            description=(
                "Use this when you want to call a deferred tool that isn't in "
                "your baseline schema set yet -- call load_tools with its group "
                "first, in the same turn, before invoking that tool. This is "
                "not a normal working tool: it doesn't touch tasks or data, "
                "just exposes more schemas for the rest of this turn. Only "
                "relevant to the OpenAI-style tool loop (the eager/deferred "
                "split doesn't apply over MCP, where every tool is already "
                "exposed). Groups: task_lifecycle (dispatch_task, "
                "record_verdict, approve_gate, cancel_task, request_review, "
                "update_task, archive_task, generate_spec_plan), admin "
                "(project/agent/knowledge/settings management), session "
                "(compact_context), research (get_minimal_context, "
                "get_impact_radius), query (get_task_events, suggest_agents), "
                "spec (impl_design, spec_write, spec_get, spec_stale). If a "
                "tool call still fails as unrecognized after loading its "
                "group, double check the group name against this list and "
                "load_tools again with the right one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "enum": list(DEFERRED_GROUPS),
                        "description": "Tool group to load.",
                    },
                },
                "required": ["group"],
            },
            handler="load_tools",
            tier="eager",
            permission="read",
            entity="meta",
            slash_alias=None,
            group="meta",
        ),
    ]
}

# Deprecated tool names that must keep routing to a canonical entry so
# sessions started before a rename don't break.
DEPRECATED_ALIASES: dict[str, str] = {
    "pm_create_task": "create_task",
}


def resolve_tool_name(name: str) -> str:
    """Map a (possibly deprecated) tool name to its canonical registry name."""

    return DEPRECATED_ALIASES.get(name, name)


def get_spec(name: str) -> ToolSpec | None:
    """Look up a registry entry by canonical or deprecated tool name."""

    return TOOL_REGISTRY.get(resolve_tool_name(name))


def get_by_slash_alias(alias: str) -> ToolSpec | None:
    for spec in TOOL_REGISTRY.values():
        if spec.slash_alias == alias:
            return spec
    return None


def to_openai_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Project ``ToolSpec`` entries into OpenAI/Anthropic-style function schemas."""

    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }
        for spec in specs
    ]


def get_group_tool_definitions(group: str) -> list[dict[str, Any]] | None:
    """OpenAI schemas for the deferred tools in ``group`` (ADR-001 §D3).

    Returns ``None`` for a group name outside :data:`DEFERRED_GROUPS`. An
    empty list is a valid result for a group with no tools yet.
    """

    if group not in DEFERRED_GROUPS:
        return None
    deferred = [
        spec
        for spec in TOOL_REGISTRY.values()
        if spec.tier == "deferred" and spec.group == group
    ]
    return to_openai_tools(deferred)


def get_group_for_tool(tool_name: str) -> str | None:
    """Return the deferred group containing ``tool_name``, if any."""

    spec = get_spec(tool_name)
    return spec.group if spec is not None and spec.tier == "deferred" else None


def get_mcp_tool_specs() -> list[ToolSpec]:
    """Tools exposed over the MCP projection for the coordinator chat CLI
    (ADR-001 §D5).

    MCP-connected CLIs manage their own context budget, so the eager/deferred
    split that exists for the OpenAI tool loop doesn't apply here: every
    registry tool is exposed except ``load_tools``, which is a mechanism
    specific to that loop and meaningless over MCP.
    """

    return [spec for spec in TOOL_REGISTRY.values() if spec.name != "load_tools"]


def dump_registry() -> list[dict[str, Any]]:
    """Registry dump for ``GET /api/tools`` (UI tool palette / ``/help``)."""

    return [
        {
            "name": spec.name,
            "description": spec.description,
            "slash_alias": spec.slash_alias,
            "tier": spec.tier,
            "group": spec.group,
        }
        for spec in TOOL_REGISTRY.values()
    ]
