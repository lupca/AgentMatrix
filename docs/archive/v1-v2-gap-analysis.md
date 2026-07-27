# Control Tower v1 → v2 Gap Analysis

**Task:** CTV2-046
**Date:** 2026-07-26
**Compared:** file-over-API Control Tower v1 against the current Control Tower v2 working tree (`362e9c1`)
**Decision:** v2 is not yet at workflow parity with v1 and cannot currently substantiate either the 80% token-reduction target or an accuracy improvement.

## Executive summary

V2 has the better operational foundation: PostgreSQL is a more reliable source of truth than cross-linked Markdown, database aggregation can replace repeated file scans, agent execution is durable, output can be streamed and replayed, and the UI exposes work in a much more usable form. Those are material gains.

The control plane, however, is not yet authoritative. V2 currently has three divergent workflow implementations:

1. a compiled LangGraph using simple nodes from `backend/app/graph/nodes.py`;
2. richer but largely disconnected nodes in `backend/app/graph/gates/`;
3. direct REST, chat-command, UI, and worker mutations that bypass both graphs.

This is the central gap. The production dispatch path does not validate gates or mode, a successful executor run sets a task directly to `done`, the chat verdict command can close any task without a reviewer or result reference, and the generic task PATCH endpoint accepts direct lifecycle changes. The compiled graph also clears its own supervised approval and continues through review to a default passing verdict in one invocation. Consequently, v2 can record a task as completed without an independent review.

The database inequality constraints are a useful backstop against storing the exact same non-null executor and reviewer. They do not require a reviewer, do not require a real result reference before `done`, and do not stop the worker or verdict endpoint from completing a task with `reviewer = NULL`. Four-eyes is therefore present as a local invariant but absent as an end-to-end guarantee.

The v1 knowledge, prediction, agent-performance, rejection-rotation, causal-analysis, tool-preflight, ingest, lint, and reconciliation mechanisms are mostly missing. V2's migration script imports only projects, tasks, and agents, so the knowledge and audit history required to reproduce v1 behavior are not migrated.

The current implementation may use fewer tokens in paths that substitute static defaults for reasoning, but that is not a valid efficiency win: quality controls have been removed from the measured path. There is no token ledger, cost telemetry, or quality-linked benchmark. The 80% reduction target is therefore **unverified**, not achieved.

### Release recommendation

Do not treat v2 as the authoritative replacement for v1 until the following critical conditions are met:

- all mutations pass through one transactional orchestration service;
- executor completion stops at “ready for review,” never `done`;
- supervised gates genuinely pause and record an explicit decision;
- review-order and verdict enforce status, result reference, reviewer independence, and idempotency;
- all state changes create durable audit and gate records;
- migration includes v1 knowledge and history and proves reconciliation;
- token savings and accuracy are measured on the same fixed task corpus.

## Scope and method

The review covered every requested v1 source:

- `AGENTS.md` and `AGENTS-REFERENCE.md`;
- all 12 Python files under `scripts/`, plus `update-agent-stats.sh`;
- every skill and nested reference under `.claude/skills/`;
- all 51 files under `knowledge/`, comprising 23 agent profiles and 28 knowledge, decision, guide, tool, pattern, metric, and research documents.

The v2 review covered the backend models, schemas, API routes, services, LangGraph state/builders/nodes/gates/routers, background workers, migrations, frontend task/dashboard behavior, and automated tests. It also included a read-only snapshot of the local PostgreSQL data and a run of the backend test suite.

Status labels in this report mean:

- **Full:** the v1 guarantee exists on all relevant v2 entry points and has proportionate tests.
- **Partial:** some data model or implementation exists, but the end-to-end guarantee is incomplete.
- **Missing:** no equivalent behavior was found.
- **Regressed:** v2 allows an outcome v1 explicitly prevented.
- **Superseded:** v2 provides an intentionally better replacement without losing the guarantee.

Severity labels mean:

- **Critical:** can produce an incorrect completion, bypass governance, lose required source data, or invalidate migration claims.
- **Medium:** materially reduces accuracy, observability, or operating effectiveness without by itself allowing false completion.
- **Low:** documentation, consistency, or usability debt after correctness is restored.

## V1 reference behavior

### Lifecycle and gates

V1's canonical lifecycle is:

```text
todo → dispatched → in-review → done
                       │
                       └─ changes-requested → dispatched
```

The lifecycle is implemented as a series of explicit operator commands and mechanical scripts:

- `/pm` creates the specification and implementation plan.
- `/dispatch` validates the task, agent, project, CLI, and repository and moves `todo` or `changes-requested` to `dispatched`.
- `/review-order` requires a dispatched result, assigns a different reviewer, persists a review sheet, and moves to `in-review`.
- `/verdict` validates the review and either moves to `done` or records changes and returns the task to execution.

The workflow is not merely a status sequence. Each gate adds evidence:

- spec: concrete acceptance criteria, files, tests, dependencies, flows, knowledge gaps;
- plan: semantic and structural graph context, impact radius, relevant patterns, and verification strategy;
- dispatch: a resolved executor profile, model/effort/CLI, repository, and safely quoted command;
- review-order: a persisted, task-specific review sheet with acceptance criteria, Definition of Done, tests, and graph questions;
- verdict: review findings, a real result reference, acceptance-criteria results, rejection count, performance outcome, prediction outcome, and causal fields where risk requires them.

### Operating modes

V1 has a global mode in `state/mode.md`, re-read at every gate:

| Mode | Required behavior |
|---|---|
| `supervised` | Stop for operator approval at every gate. |
| `plan-only` | Permit spec, plan, and review-order preparation but block dispatch and verdict execution. |
| `bypass` | Continue automatically, while recording each gate as auto-approved. |

Protected actions remain interactive even in bypass mode: deleting a task, deleting a project, or making a bulk change affecting more than three items.

### Accuracy controls

V1's `/pm` workflow requires more than a generic plan:

- minimal-context loading rather than indiscriminate repository reading;
- semantic search;
- dependency and impact-radius queries;
- `tests_for` and affected-flow discovery;
- knowledge-gap detection;
- hub/bridge analysis for the top 50 nodes;
- matching against recurring patterns and decisions;
- optional OCR preflight and mandatory graph preflight;
- task splitting when the plan exceeds eight meaningful steps;
- a pre-execution success prediction and confidence interval;
- a verifier pass before execution.

Graph/tool unavailability is classified as hard or soft by the tool registry. Required preflight cannot silently degrade into generic content.

### Four-eyes, rejection, and causal controls

V1 hard-refuses a reviewer who is the executor. It does not silently substitute a reviewer or allow an absent reviewer at verdict. A passing verdict requires a real commit/result reference.

After a rejection:

- the task returns to execution with explicit findings;
- rejection count is incremented;
- reviewer rotation is required after two rejections;
- high-risk failures require root cause, why-not-caught, and prevention fields;
- recurring causes can update the pattern library.

### Agent statistics and prediction

V1 agent profiles track:

- total executed and reviewed;
- success rate;
- average review rounds;
- strengths and weaknesses;
- trend;
- last activity.

The prediction record is created before execution, then compared with the review outcome so calibration can improve. The v1 profile corpus contains some internal drift between frontmatter totals and narrative totals; that is a reason to migrate raw events and recompute summaries, not to discard the behavioral requirement.

### Knowledge and audit

V1 knowledge is typed and operational:

- ADRs capture decisions and consequences;
- guides describe mandatory procedures;
- patterns capture recurring success and failure modes;
- the tool registry defines discovery and failure policy;
- agent profiles are used for assignment;
- metrics record prediction accuracy;
- indexes and links define discoverability and relationships.

The v1 audit log is append-only and records structured lifecycle actions, actors, timestamps, decisions, and details.

## V1 executable inventory

### Scripts

| V1 file | Responsibility | V2 equivalent | Status |
|---|---|---|---|
| `scripts/ct_common.py` | Shared frontmatter parsing, mutation, list handling, and task lookup | SQLAlchemy models and service-specific helpers | **Superseded/partial** — database access is better, but lifecycle invariants are not centralized. |
| `scripts/parse_frontmatter.py` | Robust YAML/frontmatter parsing for migration | `scripts/migrate_md_to_db.py` parsing helpers | **Partial** |
| `scripts/ct-dispatch.py` | Validates lifecycle, task, agent, project, CLI and repository; constructs safely quoted command; transactionally updates state; supports dry run | `backend/app/api/dispatch.py`, `command_builder.py`, `agent_runner.py` | **Regressed** — durable execution is better, but gate/mode validation is absent and chat dispatch queues an `echo` in `/tmp`. |
| `scripts/ct-review-order.py` | Requires dispatched task and result reference, hard-enforces four-eyes, persists review sheet, transitions to `in-review`, supports dry run | Simple graph node and unused richer review gate | **Missing/regressed** |
| `scripts/ct-report-stats.py` | Mechanical project/status aggregation and project progress regeneration | `/api/stats/*` and dashboard | **Superseded/partial** — DB aggregation is better, but frontend field/status mappings are incomplete. |
| `scripts/ct-validate-skills.py` | Validates skill metadata and frontmatter | No equivalent | **Missing** |
| `scripts/ct-verdict-apply.py` | Validates verdict prerequisites; scoped AC ticking; review-sheet update; prediction calibration; rejection alerts; causal/pattern update; executor/reviewer stats; dry run and transactional rollback | Chat verdict, simple/richer graph verdict nodes | **Regressed** |
| `scripts/update-agent-stats.sh` | Legacy/fallback profile stat updater | Agent fields and stats API | **Partial** — process success and assignment counts do not represent review quality. |
| `scripts/add-review-frontmatter.sh` | One-time, dry-runnable normalization of legacy review files into the review-sheet schema | Database migration/backfill | **Superseded/partial** — a database backfill is preferable, but v2 has no equivalent review-artifact migration. |
| `scripts/migrate_md_to_db.py` | Migrates projects, tasks, agents, all knowledge, and audit records | v2 `scripts/migrate_md_to_db.py` | **Regressed** — v2 imports only projects, tasks, and agents. |
| `scripts/test_ct_dispatch_review.py` | Dispatch/review lifecycle and four-eyes verification | API, graph, and integration tests | **Partial** — components are tested, but production end-to-end semantics differ. |
| `scripts/test_ct_validate_skills.py` | Skill validator tests | No equivalent | **Missing** |
| `scripts/test_ct_verdict_apply.py` | Verdict, idempotency, stats, prediction, and causal behavior tests | Graph/API tests | **Regressed** |
| `scripts/test_migrate_md_to_db.py` | Full migration and idempotency tests | v2 migration tests | **Partial** — only the reduced v2 scope is tested. |

### Skills and nested references

| V1 skill | Required behavior | V2 status |
|---|---|---|
| `pm` + `task-creation.md` + `task-execution.md` | Spec/plan gates, graph/tool preflight, pattern use, prediction, verifier, task splitting, concrete execution/review instructions | **Regressed** — `/pm` creates only a bare task; richer plan nodes are not on the runtime path. |
| `dispatch` | Resolve agent configuration and safely launch only after gate approval | **Partial/regressed** |
| `review-order` | Independent reviewer selection and persisted review artifact | **Missing** |
| `verdict` | Mechanical guarded verdict and all downstream records | **Regressed** |
| `mode` | Global supervised/plan-only/bypass behavior, re-read per gate | **Missing/regressed** |
| `ingest` | Reconcile external work with projects/tasks/knowledge | **Missing** |
| `report` | Mechanical aggregate reporting | **Superseded/partial** |
| `lint` | Fourteen repository/control-plane health checks | **Missing** |
| `goal` | Goal proof-of-concept workflow | **Missing** |

### Knowledge inventory

| V1 corpus | Files reviewed | Operational role | V2 status |
|---|---:|---|---|
| Root index | `_index.md` | Typed discovery map across agents, decisions, guides, patterns, tools, metrics, and research | **Missing** as relationships; generic category filtering is only a partial replacement. |
| Agent profiles | 23 profiles spanning coordinator/operator/human identities and Antigravity, Claude, Gemini, GPT, and Sonnet variants | Dispatch configuration plus execution/review evidence, strengths, weaknesses, trend, and activity | **Partial/regressed** |
| ADRs | ADR-001 through ADR-011: file-over-API; roadmap; CLI orchestration; removing Obsidian; archiving dormant guidance; modes/gates/states; mechanical report; mechanical verdict; mandatory tool registry; dispatch/review scripts; skill validation | Decision history and non-negotiable workflow rationale | **Missing** from migration and runtime retrieval |
| Guides | `review-toolchain.md`, `setup-code-review-graph.md`, `setup-crg-daemon-autostart.md`, `spawn-patterns.md` | Tool setup, availability expectations, review conventions, and safe agent spawning | **Missing/partial** |
| Metrics | `prediction-accuracy.md` | Pre-execution prediction records and calibration | **Missing** |
| Pattern library | root and cross-repo indexes plus mandatory-tool-preflight, memory-leak, missing-index, N+1, and race-condition patterns | Plan/review hints and recurrence-based organizational learning | **Missing** as typed behavior |
| Research | discount/promotion architecture, headless CLI orchestration, and manual-flow token baseline | Reusable domain research and the token baseline | **Missing** from migration; token research is not connected to telemetry |
| Tool registry | `tool-registry.md` | Required/optional tool discovery, preflight, and hard/soft failure policy | **Regressed** — wrappers exist, but failure is silently converted to an empty result. |

The review treated the 23 agent files as evidence-bearing profiles rather than authoritative aggregate totals because several frontmatter and narrative totals have drifted. A v2 migration should retain the raw source and provenance, then recompute current statistics from immutable execution and review events.

## V2 architecture: what is working

V2 should retain the following gains:

1. **PostgreSQL source of truth.** Structured task, project, session, gate, audit, agent, and run entities eliminate much of the fragile Markdown mutation logic.
2. **Database aggregation.** Status and project statistics can be computed without loading task files into an LLM context.
3. **Durable execution.** `AgentRun`, Dramatiq, and Redis support queued/running/completed state, retry, timeout, duplicate-active-run prevention, cancellation, and process-group cleanup.
4. **Observable execution.** Output chunks and SSE provide live streaming, replay, and reconnect behavior that v1 did not offer.
5. **Initial database guard.** `Task` and `GateRecord` have executor/reviewer inequality constraints in `backend/app/db/models.py:48` and `backend/app/db/models.py:80`.
6. **Agent matching.** The matcher combines capability, load, cost, and observed run results with an explainable score.
7. **UI and chat surfaces.** Operators can inspect work, runs, tasks, projects, and agents without navigating a Markdown hierarchy.
8. **Graph query wrapper and cache.** The MCP client exposes semantic, impact, test, and affected-flow queries with a TTL cache.

These improvements are compatible with v1's governance model. The recommended migration plan preserves them and places the missing controls around them.

## Detailed capability comparison

| Capability | V1 guarantee | Current v2 behavior | Status | Severity |
|---|---|---|---|---|
| Canonical lifecycle | Only valid transitions through explicit gates | Status is a free string and can be patched directly | **Regressed** | **Critical** |
| Spec gate | Concrete AC, files, tests, flows, dependencies and verification | Runtime node supplies generic AC when absent | **Regressed** | **Critical** |
| Plan gate | Graph-informed, pattern-informed, verifier-checked plan | Runtime node supplies a generic three-step plan | **Regressed** | **Critical** |
| Supervised mode | Real pause and explicit approval at every gate | Runtime approval node clears its own flag and continues | **Regressed** | **Critical** |
| Plan-only mode | Allows planning/review-order prep; blocks dispatch and verdict | Compiled router only stops after plan; APIs ignore mode | **Partial/regressed** | **Critical** |
| Bypass mode | Auto-approve with audit entry; protected actions still stop | APIs ignore mode; auto-approval records are absent | **Missing** | **Critical** |
| Dispatch validation | Checks status, gate, mode, agent, CLI, project and repository | REST checks task/agent and duplicate run, but not lifecycle/gate/mode; chat dispatch is a placeholder | **Regressed** | **Critical** |
| Execution durability | Shell launch with recorded state | Queues, retries, cancellation, timeout, SSE and output persistence | **Superseded** | — |
| Executor completion | Produces result for review, not task completion | Successful process writes task `done` | **Regressed** | **Critical** |
| Review order | Requires result, hard-refuses same reviewer, persists review sheet | No API/command; graph auto-selects a reviewer and continues | **Missing/regressed** | **Critical** |
| Four-eyes | Independent reviewer required before verdict | Inequality constraint only when both values are present; null reviewer may pass | **Partial/regressed** | **Critical** |
| Passing verdict | In-review + reviewer + real commit + completed review evidence | Chat command writes `done` based only on task id and `pass` | **Regressed** | **Critical** |
| Changes verdict | Findings persist and task loops to execution | Only status mutation in chat path | **Regressed** | **Critical** |
| Verdict idempotency | Re-verdict does not double-count metrics/patterns | No outcome-event/idempotency model | **Missing** | **Medium** |
| Rejection rotation | Rotate reviewer after two rejections | No review-attempt or rejection counter | **Missing** | **Medium** |
| High-risk causal analysis | Root cause, why-not-caught, prevention required | No risk-conditioned verdict requirement | **Missing** | **Medium** |
| Pattern recurrence | Match during planning; bump recurrence after relevant failure | Knowledge CRUD has no typed pattern behavior | **Missing** | **Medium** |
| Tool preflight | Required tool failures are explicit; no silent fallback | Graph wrapper catches failures and returns empty lists | **Regressed** | **Critical** |
| Knowledge retrieval | Typed, linked and gate-relevant documents | Flat CRUD; service returns broad project rows and is not integrated into gates | **Regressed** | **Medium** |
| Impact/flow analysis | Eight planning/review query classes, including gaps and hubs | Four wrapper query types exist | **Partial** | **Medium** |
| Pre-execution prediction | Recorded before execution with confidence | Richer unused verdict node derives prediction after findings/outcome | **Regressed** | **Medium** |
| Prediction calibration | Expected vs actual accumulated over time | No prediction-outcome entity or calibration | **Missing** | **Medium** |
| Agent performance | Verdict-derived execution/review totals, first-pass quality and rounds | Process exit success and assignment/done ratios | **Regressed** | **Medium** |
| Audit trail | Append-only record of every material action | Model/API exist; dispatch, worker, chat and graph paths omit durable audit | **Regressed** | **Critical** |
| Gate evidence | Persisted artifacts and state transitions | `GateRecord` exists but is not the authoritative transition ledger | **Partial** | **Critical** |
| Migration completeness | Projects, tasks, agents, knowledge and audit | Projects, tasks and agents only; several task fields omitted | **Regressed** | **Critical** |
| Reporting | Mechanical counts and project progress | DB aggregation available, UI mappings incomplete | **Partial/superseded** | **Medium** |
| Ingest/reconciliation | Route and reconcile incoming work | No equivalent | **Missing** | **Medium** |
| Health lint | Fourteen checks for stale, invalid and inconsistent control-plane data | No equivalent | **Missing** | **Medium** |
| Protected deletion/bulk changes | Always requires approval | Project/agent/knowledge deletion is unprotected; no real auth boundary | **Regressed** | **Critical** |
| Goal workflow | Goal state/proof of concept | No equivalent | **Missing** | **Low** |
| Token observability | V1 baseline research exists | No provider token/cost ledger; UI token values are absent or zero | **Missing** | **Critical** for the stated target |

## Critical findings

### C1. There is no canonical workflow mutation path

The compiled graph imports the simple nodes from `backend/app/graph/nodes.py`, not the richer implementations under `backend/app/graph/gates/` (`backend/app/graph/builder.py:8-18`). The builder then connects dispatch directly to review-order and review-order directly to verdict (`builder.py:109-133`).

Meanwhile, neither the normal dispatch API, command router, worker, nor generic task API invokes that graph as the authoritative transaction coordinator. This creates multiple definitions of a valid transition:

```text
UI PATCH ───────────────┐
REST dispatch ──────────┤
Chat commands ─────────┼─> tasks.status/current_gate
Agent worker ───────────┤
Simple LangGraph ───────┤
Unused rich gates ──────┘
```

Because every branch has different validation and side effects, tests of one branch do not prove the safety of another.

**Required fix:** create one application-level orchestration/transition service. REST routes, chat commands, UI actions, workers, and graph nodes must call it rather than mutate task fields. Remove or archive the duplicate gate implementation once behavior is consolidated.

### C2. Supervised approval is not a pause

The simple spec node marks a supervised task as awaiting approval, but the immediately following approval node unconditionally clears the flag (`backend/app/graph/nodes.py:62-82`). The router then continues to the plan (`backend/app/graph/router.py:18-35`). Unless callers explicitly compile with external interrupts, a supervised invocation approves itself.

The richer `check_gate_approval` is also unsafe as a foundation: its callers mutate gate output before approval, it does not implement the full plan-only contract, and interrupt/error handling can degrade into approval. It is not used by the compiled builder in any case.

**Required fix:** represent a gate decision as an immutable database record with `pending`, `approved`, `rejected`, actor, timestamp, mode, input hash, and output reference. A supervised transition must commit the pending state and return. Only a separate authenticated decision request may resume it.

### C3. Executor success falsely completes the task

On a zero-exit execution, the worker parses a result reference and calls `_update_task_status(..., "done")` (`backend/app/workers/agent_runner.py:210-216`). This skips review-order, independent review, verdict, acceptance-criteria evaluation, rejection handling, and performance/prediction outcomes.

The correct postcondition of successful execution is **ready for review**, not **done**.

**Required fix:** successful execution should persist the immutable `AgentRun` result and transition the task to a reviewable dispatched state, for example `awaiting-review`. It should then create or enable review-order. Only the verdict service may write `done`.

### C4. Verdict can bypass every v1 prerequisite

The chat command accepts only `<task_id> <pass|changes>` and writes the status (`backend/app/services/command_router.py:165-188`). It does not require:

- current status `in-review`;
- a reviewer;
- an executor/reviewer comparison;
- a real result reference;
- a persisted review sheet;
- acceptance-criteria results;
- findings for changes;
- an explicit gate decision;
- prediction, stats, causal, or audit side effects.

The generic PATCH route similarly applies every supplied task field with `setattr` (`backend/app/api/tasks.py:161-188`), allowing clients to bypass the lifecycle.

**Required fix:** remove status, gate, executor, reviewer, verdict, result, and mode from generic mutation schemas. Expose intent-specific commands with compare-and-set transition predicates and idempotency keys.

### C5. Four-eyes is a nullable inequality, not a completion invariant

The database constraint says, in effect, “if both executor and reviewer exist, they must differ” (`backend/app/db/models.py:48-60`). It does not say:

```text
status = done ⇒
  executor is present
  AND reviewer is present
  AND normalized(executor) != normalized(reviewer)
  AND result_ref is present
  AND a passing verdict record exists
```

The worker completes with no reviewer, and the chat verdict does the same. Equality checks are also exact strings, so identifier normalization belongs at the identity layer.

The richer review gate silently substitutes `@reviewer` if the supplied reviewer equals the executor. V1 hard-refuses the operation so the operator sees the governance violation.

**Required fix:** enforce the completion implication in the transition transaction and, where practical, with database constraints/triggers over authoritative verdict data. Use immutable principal IDs rather than display strings. Never silently repair a four-eyes violation.

### C6. Graph/tool failure silently lowers plan quality

The graph client catches connection or tool errors and returns empty collections. That makes “there are no dependencies/tests/flows” indistinguishable from “the required analysis did not run.” V1's tool registry deliberately distinguishes hard and soft failures and forbids silent fallback for required planning checks.

Only four query classes are wrapped; v1 also requires minimal context, knowledge gaps, hub/bridge analysis, and suggested review questions. The current `/pm` command does not call even the four available queries.

**Required fix:** persist a preflight record for every required tool/query with `success`, `empty`, `unavailable`, `timed_out`, or `skipped` and a reason. A hard-required failure must stop the gate. Empty successful results must remain distinguishable from errors.

### C7. Migration does not migrate the v1 control plane

The v2 migration scans and upserts only projects, tasks, and agents. It does not migrate:

- ADRs, guides, patterns, tool registry, metrics, research, or indexes;
- audit history;
- task body/history and several task fields;
- review findings and sheets;
- prediction outcomes;
- relationships such as `depends_on`;
- provenance and source revision.

The v1 migration utility already handled knowledge and audit, so v2 is a functional regression rather than an unavoidable limitation.

**Required fix:** make migration manifest-driven and rerunnable. Import every source file with source path, content hash, source revision, type, scope, and links. Reconcile entity counts, identifiers, required fields, relationship counts, and per-file checksums. Preserve legacy self-review data as flagged historical evidence rather than quietly nulling it.

### C8. Audit records cover only a minority of material actions

`AuditLog` exists, and task creation/update routes create some entries. Dispatch, queueing, run start/end, cancellation, approval, review-order, verdict, bypass decisions, worker status changes, and chat commands are not consistently recorded. `sync_to_db` and `log_action` in the simple graph only write logger messages (`backend/app/graph/nodes.py:50-60`).

A log line is not an audit trail: it is not transactionally tied to state and is not queryable as the source of a decision.

**Required fix:** use a transactional audit/outbox pattern inside the canonical transition service. Every accepted or rejected intent should record actor, action, prior/new state, reason, mode, correlation/idempotency key, related run/gate/verdict IDs, and timestamp.

### C9. The 80% token target has no measurement system

No durable schema records provider, model, prompt tokens, completion tokens, cached tokens, cost, or template version. The dashboard has token-oriented presentation fields but the API does not supply the underlying ledger. An unmeasured target cannot be accepted.

**Required fix:** implement the telemetry and benchmark described in “Token-reduction analysis.”

## Medium-severity findings

### M1. Agent performance measures process completion, not work quality

V2's matcher has a useful scoring structure, but its historical performance signal comes from `AgentRun` process results. A zero exit code is not a passing review. The stats endpoint derives counts from task assignments/statuses and does not preserve execution attempts, review attempts, first-pass success, rounds, reviewer outcomes, strengths, weaknesses, or trend.

Create immutable `ReviewAttempt` and `Verdict` records. Compute agent statistics from these events. Keep runtime reliability as a separate metric instead of conflating it with implementation quality.

### M2. Prediction occurs after information it is supposed to predict

The richer, unused verdict node calculates a category from review findings and risk. That is outcome classification, not pre-execution prediction. There is no confidence interval, prediction event time, frozen factor set, or actual-outcome link.

Create a prediction immediately before dispatch from the approved spec/plan, agent match, risk, impact, and analogous historical outcomes. Freeze it, then attach the independent verdict as its actual outcome.

### M3. Knowledge is flat and disconnected

`KnowledgeItem` provides generic category/content/tags CRUD, but it does not preserve typed behavior, revisions, links, provenance, applicability, recurrence, gate relevance, or tool-failure policy. The knowledge service is not integrated into the production planning/review path.

Use document revisions plus typed metadata for ADR, guide, pattern, tool, metric, research, and agent evidence. Retrieval should be scoped by project, gate, category, graph neighborhood, and top-k relevance.

### M4. Rejection learning is absent

There is no authoritative rejection count, review-attempt history, rotation rule, high-risk causal record, or pattern recurrence update. Repeated failure therefore does not improve future assignment or planning.

### M5. Reporting APIs and dashboard contracts diverge

Database aggregation is the right replacement for v1 file scanning, but the dashboard expects fields the overview endpoint does not return and maps lifecycle names such as `in_review`/`completed` while stored data uses `in-review`/`done`. Missing token and activity fields display zero rather than “unavailable.” This can give operators a falsely reassuring view.

Publish one typed status enum and generated API contract to backend and frontend. Never fabricate an operational metric when the source is absent.

### M6. Task prediction UI fabricates confidence

When task prediction data is missing or categorical, the task metadata component falls back to a percentage and generic factors. Missing evidence must be displayed as “not predicted,” not as an apparently measured confidence.

### M7. Ingest and reconciliation are missing

V1 could route incoming information into projects, tasks, or knowledge and reconcile it with existing state. V2's chat surface creates tasks but does not provide a comparable provenance-preserving ingest workflow.

### M8. Repository/control-plane lint is missing

V1's fourteen checks catch stale links, invalid frontmatter, broken task relationships, status inconsistencies, missing results, and governance drift. A database does not eliminate these semantic problems. Implement them as scheduled health queries and transition invariants.

### M9. Test coverage proves components, not parity

The backend suite result for this review was:

```text
157 passed, 2 failed, 1 skipped
```

The failures were both MCP integration related:

1. the mocked handshake expected `/usr/bin/python3`, which is absent in the container;
2. the graph-client cache test failed before the patched tool call because MCP connection setup returned an empty result.

More importantly, several passing tests encode the current divergence. Graph-node tests expect a bypass invocation to reach `done` in one call, and the “full flow” integration test manually moves a task to `in-review` before verdict rather than proving that real executor completion creates and survives an independent review. Add black-box invariant tests across every public entry point.

## Low-severity findings

### L1. Dead and duplicate workflow code obscures the intended design

The simple graph nodes, richer gate package, direct command handlers, and direct APIs use the same gate vocabulary while implementing different contracts. Beyond the correctness problems already classified as critical, this increases maintenance and review cost. Consolidate first, then remove unused implementations and update architecture documentation.

### L2. Legacy terminology and status values remain visible

The local data contains both `completed` and `done`, while frontend components also use underscore variants for hyphenated states. Canonical enums and a one-time normalization migration will reduce confusion after lifecycle enforcement is in place.

### L3. The goal proof of concept has no v2 replacement

The v1 `goal` skill is outside the seven primary workflow skills in CTV2-046 and is not required to prevent false task completion. Preserve its source during migration and schedule a product decision on whether to implement or explicitly retire it.

## Direct answers to the task questions

### 1. Are all v1 workflow gates implemented?

No. Names for spec, plan, dispatch, review-order, and verdict exist, but the compiled nodes do not reproduce v1 behavior, the richer alternatives are not the builder's nodes, and production API/worker entry points bypass the graph. Spec and plan can use generic defaults; review-order is not an operator-facing persisted workflow; verdict lacks prerequisites.

### 2. Is four-eyes enforced?

Only partially. Exact executor/reviewer equality is rejected when both are non-null in `Task` and `GateRecord`. A reviewer is not required for `done`, and current worker/verdict paths complete tasks with no reviewer. End-to-end four-eyes is therefore not enforced.

### 3. Are agent statistics preserved?

No. V2 preserves a single `success_rate` field and derives some assignment/completion counts. It does not preserve or correctly recompute v1's verdict-derived execution/review totals, first-pass quality, review rounds, strengths/weaknesses, trend, or last-activity semantics.

### 4. Is success prediction implemented?

No, not in the v1 sense. The relevant richer node calculates a category after review information exists, and it is not on the compiled runtime path. V2 has no pre-execution immutable prediction or calibration loop.

### 5. Is knowledge integrated?

Only as flat CRUD and a partially implemented graph wrapper. V1's typed corpus is not migrated, and neither knowledge nor graph evidence is integrated into the normal `/pm`, dispatch, review-order, or verdict path.

### 6. Are supervised, plan-only, and bypass modes preserved?

No. Mode is a per-task string, not the v1 global policy re-read at every gate. APIs ignore it. Supervised does not reliably pause, plan-only is only recognized by the simple graph after plan, bypass decisions are not audited, and protected actions remain unguarded.

### 7. Is the audit trail complete?

No. The table and read API exist, but most material transitions do not write it. The graph's “audit” node logs to the process logger only. The local snapshot also demonstrates low usage relative to task volume.

## Local database snapshot

A read-only inspection of the running local database found:

| Entity/state | Count |
|---|---:|
| Projects | 17 |
| Tasks | 152 |
| Agents | 20 |
| Knowledge items | 11 |
| Audit entries | 7 |
| Gate records | 1 |
| Sessions | 7 |
| Agent runs | 0 |
| `todo` tasks | 35 |
| `dispatched` tasks | 6 |
| `in-review` tasks | 4 |
| `done` tasks | 106 |
| legacy `completed` tasks | 1 |

All 152 tasks were in `supervised` mode, but the database contained only one gate record and seven audit rows, all with action `update_task`. The 11 knowledge rows were duplicate fixture-like categories rather than the 28 non-agent v1 knowledge documents. This snapshot is diagnostic of the current local deployment, not a claim about every environment, but it is consistent with the static-code gaps.

The presence of both `done` and `completed` also confirms that task status is not constrained to one canonical enum.

## Token-reduction analysis

### V1 baseline

V1's recorded manual-flow baseline from 2026-07-22 estimated:

- 264 mandatory core instruction lines;
- 459 on-demand reference lines;
- 390 skill lines;
- approximately 2,750 lines or 3,575 input tokens for one complete cycle before reasoning and output;
- reasoning/output overhead estimated at roughly 2–10 times the read-only context.

That baseline is historical. The current v1 corpus is larger, so it should not be reused as the sole acceptance baseline without replaying the same task set.

### Where v2 can reduce tokens

V2 has credible opportunities for major reductions:

- slash commands can route mechanically without an LLM;
- SQL queries can replace repository-wide task, project, audit, and report scans;
- structured task fields can avoid reparsing Markdown;
- graph results can be fetched by gate and cached;
- agent/run state can be read directly rather than inferred from logs;
- session summaries can replace replay of complete chat history;
- deterministic validation, assignment filters, and transition checks need no model tokens.

These are real architectural advantages. They do not by themselves prove an 80% reduction.

### Current regressions that can waste tokens or trade away accuracy

- The chat path resends stored session history, creating context growth over a long session.
- The dispatch command inlines raw input, acceptance criteria, files, tests, and plan rather than supplying a compact, versioned execution packet.
- Knowledge retrieval is not gate-scoped; a naive future implementation that loads all project knowledge would reproduce v1's context bloat.
- Graph errors collapse to empty data, encouraging retries or low-quality plans.
- Static/generic spec, plan, reviewer, and passing-verdict defaults produce near-zero model use by removing judgment, not by making judgment more efficient.
- No telemetry ties token use to a quality outcome, so the system cannot distinguish efficient success from cheap false completion.

### Required token ledger

Add an immutable `LLMUsage` record with:

- task, session, gate, message/run, and correlation IDs;
- provider and model;
- prompt-template name and version;
- prompt, completion, cached-input, and reasoning tokens where available;
- cost and latency;
- retry/fallback classification;
- whether the request was required, optional, or avoidable;
- related prediction, review attempt, and final verdict.

Also record the serialized execution-packet size and `AgentRun` output bytes. Do not call those values tokens unless measured or converted with a declared tokenizer.

### Acceptance benchmark

Replay the same fixed corpus of at least 20–30 representative tasks through v1 and v2. Include low/medium/high risk, single- and multi-repository impact, changes-requested loops, knowledge-heavy work, and unavailable-tool cases. Freeze repository revisions, agent/model versions, and grading rubrics.

Report median and p95 for:

- total input, output, cached, and reasoning tokens;
- tokens by gate;
- cost and latency;
- first-review pass rate;
- review rounds;
- acceptance-criteria completeness;
- graph-confirmed file/test/flow coverage;
- false-`done` rate;
- reviewer disagreement;
- escaped defects or seeded-fault detection;
- prediction calibration.

The 80% target should be evaluated as:

```text
reduction = 1 - median(v2 total tokens per accepted task)
                / median(v1 total tokens per accepted task)
```

“Accepted task” must require the same independent quality bar in both systems. Failed, bypassed, or falsely completed work cannot be excluded from one side or counted as savings.

### Token conclusion

**Current result: unverified.** V2 has a plausible path to an 80% reduction through deterministic routing, structured queries, caching, and compact context. It has no current evidence for the number, and its shortest paths omit accuracy controls. Token optimization should proceed only after the authoritative workflow prevents false completion.

## Recommended migration plan

### Phase 0 — correctness and governance

1. **Create one orchestration service.** Define allowed commands and transition predicates in one module. Make graph nodes, REST, chat, UI, and workers call it.
2. **Replace free-form lifecycle writes.** Introduce canonical enums and compare-and-set transitions. Remove controlled fields from generic PATCH.
3. **Make gates durable.** Store immutable gate attempts and explicit decisions. Supervised requests commit and pause.
4. **Fix executor completion.** Write `awaiting-review`, persist the run result, and require review-order next.
5. **Implement review-order.** Require a valid result, independently select/validate a reviewer, persist the review packet, and hard-refuse conflicts.
6. **Implement guarded verdict.** Require `in-review`, independent reviewer, real result reference, completed AC results, and a passing verdict event before `done`.
7. **Apply mode policy everywhere.** Support a global/default policy plus a task snapshot; re-evaluate current policy at each material action. Keep protected actions interactive.
8. **Make audit transactional.** Record accepted/rejected intents and all run/gate/verdict changes with actor identity and idempotency keys.
9. **Protect destructive actions.** Add authentication/authorization and explicit approval for task/project deletion and bulk changes.
10. **Add black-box invariants.** Exercise every REST, chat, UI-backed, graph, and worker path and prove no path can reach `done` without the same evidence.

### Phase 1 — accuracy and learning

11. **Wire one real spec/plan implementation.** Remove static defaults; integrate graph and knowledge queries with explicit preflight outcomes.
12. **Complete v1 graph coverage.** Add minimal-context, knowledge-gap, hub/bridge, and review-question queries in addition to semantic, impact, tests, and flows.
13. **Restore verifier and splitting.** Validate plan evidence and split work above the defined complexity threshold.
14. **Implement pre-execution prediction.** Freeze factors and confidence before dispatch; link independent outcomes for calibration.
15. **Model review attempts and verdicts.** Derive execution/review stats, first-pass rate, rounds, rejection rotation, and trends from events.
16. **Restore risk learning.** Require causal fields for high-risk changes and connect recurring causes to typed patterns.
17. **Integrate typed knowledge.** Add document revisions, provenance, relationships, applicability, recurrence, and gate-aware retrieval.
18. **Restore health checks.** Convert v1 lint rules into database constraints, scheduled checks, and an operator health view.

### Phase 2 — complete migration and product reporting

19. **Expand migration.** Import all v1 knowledge, audit, relationships, predictions, review evidence, and source metadata.
20. **Reconcile migration.** Produce a signed/retained manifest of source count, imported count, skipped count/reason, identifiers, and hashes.
21. **Restore ingest.** Add provenance-preserving reconciliation for incoming project/task/knowledge material.
22. **Fix dashboard contracts.** Share lifecycle enums and schema-generated client types; display unavailable metrics honestly.
23. **Add the token ledger and corpus benchmark.** Publish token and quality results together.
24. **Remove dead paths.** Once parity tests pass, delete the unused gate implementation, placeholder chat paths, and misleading fallback UI values.

## Proposed authoritative v2 flow

```text
Create task
    │
    ▼
Spec attempt ── required preflight failure ──> blocked/error record
    │
    ▼
Explicit approval decision (or audited bypass)
    │
    ▼
Plan attempt + verifier + prediction
    │
    ▼
Explicit approval decision (plan-only stops here)
    │
    ▼
Dispatch intent ──> AgentRun queued/running ──> awaiting-review
                                                   │
                                                   ▼
                                       Review-order attempt
                                                   │
                                  independent reviewer + packet
                                                   │
                                                   ▼
                                           Review attempt
                                          /              \
                                  changes                  pass
                                     │                      │
                          causal/pattern/stats       verdict transaction
                                     │                      │
                              redispatch loop              done
```

Every arrow that changes persistent state should be one transaction containing:

- validated prior state;
- new domain event;
- task projection update;
- gate/audit record;
- outbox message if background work follows.

## Acceptance criteria for parity

V2 should not be declared a replacement until automated tests prove all of the following:

- no public or worker path can set `done` without an independent passing verdict;
- the executor cannot be the reviewer after identity normalization;
- successful execution always stops before review;
- supervised mode pauses at every required gate and only a separate decision resumes it;
- plan-only cannot dispatch or apply a verdict through any entry point;
- bypass records each automatic decision and still blocks protected destructive actions;
- missing required graph/tool preflight stops the relevant gate;
- a passing verdict requires a real, reachable result reference;
- changes verdicts persist findings and increment exactly one review attempt;
- reapplying the same verdict is idempotent;
- reviewer rotation and high-risk causal fields are enforced;
- prediction is timestamped before dispatch and evaluated only after review;
- agent performance derives from review outcomes, not exit status;
- all material actions are auditable and correlated;
- migration reconciles every v1 source file, or records a specific justified exclusion;
- dashboard totals exactly match direct database queries;
- token results are linked to equivalent quality outcomes and meet the declared benchmark.

## Final assessment

V2 is a strong execution and presentation platform wrapped around an incomplete control workflow. PostgreSQL, background execution, SSE, caching, matching, and the UI are worth keeping. The migration should not attempt to recreate v1's Markdown mechanics; it should preserve v1's invariants and evidence model in transactional services and immutable events.

The highest-leverage action is to stop treating the graph, API, chat commands, and worker as separate state owners. Once one guarded transition service becomes authoritative, the richer planning, knowledge, review, prediction, audit, and token instrumentation can be restored without duplicating logic. Until then, claims of improved accuracy or 80% token reduction are not supportable because the current fast path can omit the very review controls that define an accepted result.
