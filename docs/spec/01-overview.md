# 01 — Tổng quan hệ thống

> AGENTMATRIX V2 (AGMX, repo `agenticmatix`): hệ điều phối coding agent qua MCP native,
> với gate-based workflow, four-eyes review, và autonomy brakes.
> Cập nhật: 2026-08-01, sau GĐ4-P1 (đã xóa hẳn lớp FastAPI/REST — MCP native là surface duy nhất).

## Kiến trúc runtime

```
Coordinator CLI (claude/agy/codex, chạy từ WORKDIR NGOÀI repo này)
  └── FastMCP client → http://localhost:8100/mcp   (Bearer token)
                              │
                              ▼
                  app.mcp_native (FastMCP server, port 8100)
                      │  make_tool_handler: auth → task-scope → CommandRouter
                      ▼
              CommandRouter (services/command_router.py)
                      │  getattr(self, f'_handle_{spec.handler}')
          ┌───────────┴───────────────┐
          ▼                           ▼
   Services (task_orchestration,   PostgreSQL :5433 (docker `agmx_db`)
   entity_admin, admin_gate,       + role `ct_readonly_user` cho query_db
   spec_plan_generator, ...)
          │
          ▼
   OutboxEvent ──► Dramatiq (Redis :6380) ──► workers/agent_runner.run_agent
                                                  │
                                                  ▼
                                    git worktree riêng (/tmp/control-tower-worktrees/...)
                                    spawn CLI: claude / agy / codex
```

## Thành phần chính (file map)

| Mảng | File |
|---|---|
| MCP server, auth, envelope, pending_approvals | `backend/app/mcp_native.py` |
| Định nghĩa tool (nguồn chân lý duy nhất) | `backend/app/services/tool_registry.py` (ADR-001) |
| Dispatch tool → handler | `backend/app/services/command_router.py` |
| Vòng đời task, gate, brakes, CAS | `backend/app/services/task_orchestration.py` |
| Admin gate (agents/projects/knowledge/settings) | `backend/app/services/admin_gate.py`, `entity_admin.py` |
| Build lệnh CLI cho executor/reviewer | `backend/app/services/command_builder.py` |
| Build lệnh CLI cho coordinator/spec-plan | `backend/app/services/cli_dispatcher.py` |
| LLM routing (CLI vs API agent) | `backend/app/services/llm_service.py`, `providers/` |
| Worker chạy run + reaper + dead-letter | `backend/app/workers/agent_runner.py`, `outbox_publisher.py`, `services/outbox.py` |
| Spec/plan generation | `backend/app/services/spec_plan_generator.py` |
| Project context & scoped rules | `backend/app/services/context_generator.py` |
| Attach MCP vào CLI lúc spawn | `backend/app/services/mcp_attach.py` |

## Nguyên tắc bất biến

1. **Four-eyes**: reviewer ≠ executor (constraint DB + `_require_independent`).
2. **GateRecord append-only**: quyết định là ROW CON (`parent_id`), parent giữ
   `status="pending"` mãi mãi. "Còn mở" = pending AND không có con.
3. **Worktree isolation**: mỗi AgentRun chạy trong git worktree riêng, commit lên
   branch `ct-run/<run_id>`; coordinator KHÔNG BAO GIỜ merge các branch này.
4. **MCP native là surface duy nhất** — không còn REST/FastAPI.
5. **Coordinator workdir nằm ngoài repo** (init bằng `scripts/init-coordinator-workdir.sh`);
   coordinator không đọc source AGMX, không đụng DB trực tiếp ngoài `query_db`.
6. **Root `.env` là env chuẩn** — `start-backend.sh` source nó với `set -a`.
7. **Không nới lỏng schema strict/extra** — sai lệch giữa prompt và schema thì sửa
   prompt hoặc sửa schema CÓ CHỦ ĐÍCH kèm test, không nới validation.

## Đọc tiếp

- `02-data-model.md` — bảng, trạng thái, vòng đời.
- `03-gates-and-autonomy.md` — gate flow, approve/reject, brakes, escalation.
- `04-tool-surface.md` — từng MCP tool và quirks.
- `05-agents-providers.md` — agent CLI/API, effort, reasoning model.
- `06-context-rules.md` — project context & scoped rules (onboarding project).
- `07-runtime-ops.md` — vận hành: start/restart, worker, migrate, sự cố đã biết.
- Backlog sống: `../plans/GD4-CLEANUP-PLAN.md` + task files ở
  `~/projects/control-tower/projects/agenticmatix/tasks/CTV2-2xx*.md`.
