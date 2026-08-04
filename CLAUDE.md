# AGENTMATRIX (AGMX) V2

Hệ thống điều phối coding agent với gate-based workflow, four-eyes review, và autonomy controls.

## Kiến trúc

```
Coordinator CLI (Outside Repo Workdir)
  └── FastMCP Client (mcp_native) → http://localhost:8100/mcp (Bearer Token Auth)
                                       │
                                       ▼
                              FastMCP Server (Port 8100)
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
              CommandRouter / Services         PostgreSQL (5433)
                        │                      ct_readonly_user
                        ▼
                 Dramatiq Worker → CLIDispatcher → agy/claude/codex
```

## Data Model chính

- **Task**: todo→dispatched→awaiting-review→in-review→done/changes-requested/failed
- **AgentRun**: queued→running→success/failed/timeout (kind: execute|review)
- **GateRecord**: append-only ledger (pending→approved/rejected)
- **Agent**: CLI-backed (claude/agy/codex) với capabilities[], success_rate
- **Session**: context_level (global|project|task), messages[]

## Gate Flow

```
todo → [dispatch gate] → dispatched → [run_agent] → awaiting-review
     → [review_order gate] → in-review → [run_agent review] → [verdict gate] → done
```

Mode: supervised (cần approve), plan-only (block dispatch), bypass (auto-approve)

## Autonomy & Brakes

```python
AutonomyPolicy(per project hoặc global Setting):
  autonomy: supervised|plan-only|auto
  auto_max_risk: low|normal
  auto_max_rounds: 3

check_brakes():
  - autonomy_enabled=false → STOP
  - authoritative API task_cost ≥ max_cost_usd_per_task → STOP
  - task_tokens ≥ max_tokens_per_task → STOP
  - active_runs ≥ max_concurrent_runs → QUEUE
```

## Commands

```bash
# Start backend (DB + Redis + FastMCP + Worker)
./scripts/start-backend.sh

# Run tests
backend/venv/bin/python -m pytest backend/tests -q
```

## Quy tắc quan trọng

- **Four-eyes**: reviewer ≠ executor (DB constraint)
- **GateRecord**: append-only, immutable
- **Worktree isolation**: mỗi AgentRun chạy trong git worktree riêng
- **MCP Native**: Coordinator CLI tương tác 100% qua tool surface FastMCP native
- **Coordinator Workdir**: Chạy coordinator CLI từ ngoài repo (`~/ct-coordinator`)

## Spec chi tiết

Đặc tả đầy đủ (và luôn phải phản ánh hiện trạng code) ở `docs/spec/01..07`
— bắt đầu từ `docs/README.md`. Đổi hành vi hệ thống thì cập nhật spec tương ứng
trong cùng PR. Backlog sống: `docs/plans/GD4-CLEANUP-PLAN.md`.

## Cho AI agent

**ĐỌC `docs/AGENT-PLAYBOOK.md` TRƯỚC KHI LÀM BẤT CỨ VIỆC GÌ** — đó là ký ức
làm việc chưng cất từ các phiên trước: vòng lặp giải quyết vấn đề, bộ đồ nghề
riêng của dự án, cách điều phối task qua chính hệ thống, các họ bug đặc trưng,
và kỳ vọng của lupca (sửa luôn không hỏi, không dùng fable/claude-review/agy,
DB là source of truth, mọi việc ghi thành task CTV2-xxx).
