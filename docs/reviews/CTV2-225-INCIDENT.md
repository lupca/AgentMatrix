# CTV2-225 — result_ref / integration-branch bypass incident

Date: 2026-08-04
Severity: CRITICAL

## Finding

The verdict and landing FSM paths are fail-closed, but the executor boundary was
not. `execute_agent_run` initialized `exec_cwd` to the project's integration
checkout. When `AGENT_RUN_USE_WORKTREE` was false, or when
`WorktreeManager.create()` raised `WorktreeUnsupportedError`, it ran the CLI in
that checkout. An ordinary executor `git commit` therefore advanced `main`
before `record_execution_success`, review, or a pass verdict. A later process,
review, idempotency, or retry failure only failed the ledger workflow; Git had
already changed.

This is a worker/executor side effect, not a result-ref or landing transition.
The old behavior is directly reproducible by forcing `WorktreeManager.create`
to fail and running an executor command which commits. Before this fix, the
existing unit test explicitly asserted that the new commit appeared in the
primary checkout. The regression now asserts the inverse: no command is
spawned, the integration HEAD is unchanged, and the run is left for the normal
retry/dead-letter lifecycle.

The valid lifecycle reproducer is
`test_integration_branch_moves_only_after_independent_pass_verdict`. It checks
the task projection, GateRecord ledger, outbox, and integration HEAD after
`record_execution_success`, `request_review`, pass verdict, and idempotent
`land_task`. In the current FSM, approving the pass verdict performs the first
merge; `land_task` is the retry/backfill surface.

## CTV2-232 and CTV2-1359 classification

Repository reflogs distinguish the three proposed causes:

- `main@{2026-08-04 10:49:26 +0700} 3c0c3e3 commit: CTV2-1359...`
- `main@{2026-08-04 11:18:39 +0700} 8a9c4a2 commit: ... (CTV2-232)`
- the linked `main-worktree/HEAD` reflog contains the same direct `commit:`
  entries.

Both commits are single-parent commits made in the primary main worktree.
They are not manual merges, because neither the commit topology nor reflog is a
merge. They are not the landing service: `land_result` uses `git merge --no-ff`
and then creates a merge commit, visible in the same reflog as `commit (merge)`
or `merge`. Thus both were direct writes in the integration checkout, matching
the worker fallback/disabled-worktree path exactly. CTV2-232's completed
executor/review audit followed by the reused
`advance:CTV2-232:review:r2` failure explains why the code survived on main
while the task had no accepted verdict: the Git side effect preceded the
failed review bookkeeping. CTV2-1359 has the same Git signature; there is no
landing signature to support an old landing path.

This fix does not change review-attempt idempotency or duplicate-run claiming;
those remain CTV2-219 scope. It removes the integration-branch consequence by
making worktree isolation mandatory even during those failures.

## The five historical commits

| Task | Commit | Classification | Retroactive action |
|---|---|---|---|
| CTV2-010 | `01d4a08` | Legacy direct commit, predates per-run worktrees and landing | Audit; do not fabricate a verdict |
| CTV2-069 | `0f62f73` | Legacy direct commit, predates per-run worktrees and landing | Audit; do not fabricate a verdict |
| CTV2-093 | `173b85f` | Legacy direct commit, predates per-run worktrees and landing | Audit; do not fabricate a verdict |
| CTV2-232 | `8a9c4a2` | Primary-worktree direct commit; review later failed | Re-review the immutable commit with an independent reviewer if formal acceptance is required |
| CTV2-1359 | `3c0c3e3` | Primary-worktree direct commit; no landing signature | Re-review the immutable commit with an independent reviewer if formal acceptance is required |

All five SHAs are already ancestors of current main, so no Git reconciliation
is necessary to preserve code. Ledger history must remain truthful and
append-only: do not backfill or forge historical pass GateRecords. If policy
requires retroactive assurance, create new audit/review work tied to each
immutable SHA and append its result prospectively. Whether to revert a finding
from such a review is a separate governed task.

## Reconciliation design (not implemented)

A separate reconciliation task could periodically compare integration history
with `tasks.result_ref` / `landed_ref` and approved pass GateRecords, emit an
append-only anomaly event for unmatched commits, and open a review task pinned
to the immutable SHA. It must never infer or write a pass verdict merely from
Git reachability, and it must not rewrite existing GateRecords. No migration or
reconciliation code is included here.
