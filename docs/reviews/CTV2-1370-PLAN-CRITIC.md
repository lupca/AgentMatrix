# CTV2-1370 — Plan critic and planner contract v2

`SpecPlanResult` v2.0 adds only fields with an enforcement point:

| Field | Enforcement point |
|---|---|
| `constraints` | Appended after positive acceptance criteria in the flat code-review contract; template generation, result parsing, and verdict validation use the merged count. |
| `evidence` | Non-empty structured citations are required by schema; the independent critic reproduces only these cited commands/files/queries. |
| `prior_art` | The critic checks targeted `spec_item`/`spec_task_link` and git history before accepting claims that work is already solved. |
| `ruled_out` | Structured approach/reason pairs are challenged by the critic. |
| `limits` | Required for high risk; task-local round, token, and optional USD ceilings tighten existing orchestration brakes. |

The critic is a second CLI model distinct from the planner. Its input contains
the task, project context, and plan JSON—never a diff or base/head range. The
prompt forbids broad reading and limits verification to cited evidence plus
targeted prior-art lookups. A 50,000-token input/output envelope is checked
before and after the call; output is capped at 4,096 tokens. A reject verdict
is schema-invalid unless every blocking finding contains reproducible evidence.

Every run is recorded as an append-only `plan_critic` ledger row. `get_stats`
reports rejection rate and extra execution rounds (`max(round_count-1, 0)`) for
historical tasks without critic versus tasks with critic. No tiered spec
selection, `spec_anchor`, or `spec_stale` behavior is included.

No proposed field was left without an enforcement point, so no decorative
field was added to the schema.
