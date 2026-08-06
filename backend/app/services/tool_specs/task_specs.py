from __future__ import annotations

from app.services.tool_specs.base import ToolSpec

TASK_TOOL_SPECS: list[ToolSpec] = [
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
                    "ask_human actively notifies a human and expects one. Once the human answers in chat, call this again with `answer` (plus task_id) to record what they said and unblock the task -- asking marks the task as waiting on a human, and nothing else can clear that mark. This is "
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
                        "answer": {
                            "type": "string",
                            "description": (
                                "Answer mode: what the human said in chat. Pass "
                                "with task_id (question/why_human not needed). "
                                "Records the answer verbatim and clears the "
                                "waiting-on-human mark this tool set, so the task "
                                "can move again."
                            ),
                        },
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
]
