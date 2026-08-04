# CTV2-1373 — live planner verification

Verified on 2026-08-04 with the real `@gpt-5.6-sol` CLI planner and the live
FastMCP server. The task was the existing `agenticmatix` todo task CTV2-224
(`update_task` accepts a governed `mode` patch).

The running backend reported `runtime_restart_required`, and this task forbids
restarting it. Therefore the live call loaded `generate_spec_plan` from this
worktree while its attached coordinator MCP connection continued to use the
running server. Raw Codex JSONL was retained in memory around the real call.

## MCP proof

The planner emitted the following tool event (twice because the generator made
its schema retry):

```json
{"type":"item.started","item":{"type":"mcp_tool_call","server":"agmx","tool":"spec_get","arguments":{"filter":{"project_id":"agenticmatix"}},"status":"in_progress"}}
```

The planner called the MCP-native tool directly; `load_tools` is intentionally
excluded from this projection and is only used by the separate OpenAI tool
loop. The matching completion returned `ok=true`, `action=spec_fetched`, and
`count=18`. The old running projection returned no top-level relations,
anchors, or task links; exposing anchors is part of the code in this change and
will take effect after a safe runtime restart.

## Grounded output proof

The resulting `prior_art` named concrete living-spec records, including:

- `spec_item:47ad3a8d-2e24-4736-81a1-30b1d411b9f4` — the existing three-mode
  design and autonomy/risk resolution.
- `spec_item:1779ef5f-de15-4b3c-8dd5-c08825b055bf` — immutable GateRecord and
  child-row decisions.
- `spec_item:ac1cf7fb-86dd-4304-8dd9-00ced8721831` — the single task-lifecycle
  mutation service and CAS path.
- `spec_item:19684fa5-62f0-4955-8eaa-7803423645fb` — Tool Registry as the
  single schema/routing source.

The resulting constraints included the concrete negative boundary:

> `spec_item:1779ef5f-de15-4b3c-8dd5-c08825b055bf`: GateRecord is append-only;
> supervised decisions must append a child row through `parent_id`, never
> update or delete the pending row.

The live project result contained no `conflicts_with` relation, so that
conditional branch could not be exercised against current project data. The
planner prompt explicitly requires any touched constraint or endpoint of a
`conflicts_with` edge to appear in `constraints`, and the prompt-order test
locks that rule before requirement/design, anchor, and task-link processing.
