# Plan: Control Tower V2 → Pure Native MCP Server

> Cập nhật 2026-08-01. GĐ1 (`61f5cca`) và GĐ2 (`cbb7328`) đã hoàn thành.
> Tài liệu này là plan hoàn chỉnh cho **Giai đoạn 3: xóa vĩnh viễn frontend + FastAPI**, chỉ còn `mcp_native` + services + Dramatiq worker.
> Nhánh `develop` (đứng ở `61f5cca`, còn đầy đủ frontend + API) chỉ là **tài liệu tham khảo** khi cần xem lại logic endpoint cũ để viết tool MCP tương đương — không phải đường lùi. Xóa là xóa hẳn.

## Kiến trúc đích

```
Database <-> app/mcp_native.py (streamable HTTP :8100) <-> N coordinator CLIs (claude/codex/agy)
                    |                                     + executor CLIs (worker spawn)
             Dramatiq Worker (agent_runner, outbox_publisher — giữ nguyên)
```

`CommandRouter.execute_tool` vẫn là điểm enforcement duy nhất (permission, gate, four-eyes).

## Các quyết định đã chốt (GĐ1–2, giữ nguyên hiệu lực)

1. Streamable HTTP, N client đồng thời; identity = token HMAC `ct1.` có `role` (coordinator/executor) + task scope.
2. Kênh workflow: `instructions` khi initialize (≤2KB, 512 ký tự đầu tự đủ) → tool description → tool result có `next` → file instruction generate từ `docs/coordinator-rules.md` ra `CLAUDE.md`/`AGENTS.md`/`PROJECT.md` (agy). Không dùng MCP Prompts/Resources.
3. Luật thật nằm server-side; lỗi trả về có cấu trúc + hint.
4. Supervised mode: human approve qua chat với coordinator CLI; GateRecord ghi `approved_by: human-via-<token>`.

---

# Giai đoạn 3 — Xóa FE + API

Kết quả audit dependency (2026-08-01): `fastapi` chỉ được import trong `app/main.py` + `app/api/*.py`; services/workers/db/graph/schemas hoàn toàn sạch, trừ **một** reverse dependency (`coordinator.py:1429` import `ws_manager`). Việc xóa phần lớn là cơ học, nhưng có **các blocker phải đóng trước** (Bước 1). Làm đúng thứ tự dưới đây.

## Bước 0 — Commit dứt điểm phần đã xóa tay

- [ ] Commit việc xóa `frontend/` (hiện đang là thay đổi chưa commit trên main).
- [ ] Xóa kèm: `e2e/`, `playwright.config.ts`, `package.json` ở root (100% phục vụ FE).

## Bước 1 — Đóng các blocker (TRƯỚC khi xóa bất kỳ file API nào)

### B1.1 — Bật native làm đường duy nhất (M3 + Q2)
Phát hiện quan trọng: `MCP_NATIVE_ENABLED` mặc định `False` (`config.py:46`) — **cấu hình mặc định hiện tại vẫn cho executor đi đường stdio-forwarder → REST**. GĐ2 chỉ "xong" khi env bật cờ này.
- [ ] Xóa hẳn cờ `MCP_NATIVE_ENABLED` và mọi nhánh non-native: `cli_dispatcher.py:192-196, 310-318`, nhánh forwarder trong `write_mcp_config`/`build_mcp_config`, `command_builder.py:26-27`.
- [ ] `cli_dispatcher` chuyển từ token tĩnh `MCP_API_TOKEN` (đang lọt qua nhánh `legacy_token` ở `mcp_native.py:74-75`) sang `issue_token(role=...)` ký HMAC như `command_builder.py:34` đã làm đúng. Sau đó **xóa nhánh `legacy_token`** trong `mcp_native.py`.
- [ ] `.env.example`: thêm `MCP_TOKEN_SECRET`, `MCP_NATIVE_URL`, `CT_MCP_PORT`; xóa `MCP_API_TOKEN`, `CT_API_URL` (Q7).

### B1.2 — Vá lỗ context staleness trên đường native (M2 — bug thật, đang tồn tại)
REST path gọi `invalidate_context_snapshot` sau mỗi tool call (`chat.py:82`); native path **không có** → snapshot context bị stale âm thầm.
- [ ] Thêm `invalidate_context_snapshot(...)` vào `mcp_native.make_tool_handler` sau khi `execute_tool` trả về, trước `db.close()` (`mcp_native.py:173-180`).
- [ ] Test: tool call qua native client → snapshot được invalidate.

### B1.3 — Cắt reverse dependency coordinator → ws (M1)
- [ ] Xóa block broadcast `ws_manager` trong `coordinator.py:1429-1446` (hiện fail-soft nhưng để lại import chết). Thay thế native: coordinator CLI đọc `TaskEvent`/`get_status` — cơ chế wake CTV2-133 đã có.

### B1.4 — Nudge sau gate approval (Q1 — cần test trước khi xóa `dispatch.py`)
`api/dispatch.py:134-139` gọi `advance_task.send(task_id, "gate_approved")` sau gate decision; `CommandRouter._handle_approve_gate` (`command_router.py:938`) **không** gọi.
- [ ] Viết test: approve gate không sinh run mới → task có tự advance không. Nếu không → port nudge vào `_handle_approve_gate`.

### B1.5 — Tool thay thế khả năng quan sát (M4 + Q5)
- [ ] Thêm tool `get_run_output` vào `tool_registry.py`: đọc `AgentOutputChunk` từ DB (replay bền, đủ cho LLM; không cần stream). Giữ nguyên Redis publish trong worker — publish không subscriber là no-op, và operator vẫn `redis-cli SUBSCRIBE` được khi cần debug live.
- [ ] Thêm tool `get_stats` (port phần cốt lõi của `api/stats.py`: token usage, cost per task/agent) — `query_db` là fallback nhưng stats có logic pricing không nên bắt LLM tự viết SQL.

### B1.6 — Launcher & health (M5 — hiện KHÔNG có gì khởi động mcp_native)
- [ ] `scripts/start-backend.sh`: thay uvicorn `app.main:app` bằng `python -m app.mcp_native --host 0.0.0.0 --port 8100`; giữ nguyên dòng dramatiq. `stop-backend.sh` đổi pattern pkill tương ứng.
- [ ] Thêm route `/health` không cần auth vào `mcp_native.py` (exempt trong `MCPAuthMiddleware` — hiện middleware 401 mọi request thiếu token nên probe không hoạt động); script/probe curl vào đó.
- [ ] `backend/Dockerfile`: `CMD alembic upgrade head && python -m app.mcp_native --port 8100`. `docker-compose.yml`: bỏ service `frontend` + `VITE_API_URL`; giữ `db`, `redis`; thêm service `mcp` + `worker`.

### B1.7 — DDL & test harness (M6 + M0)
- [ ] Xác nhận alembic phủ toàn bộ DDL (main.py:21 `create_all` chỉ là belt-and-braces). `tests/conftest.py` tự lo `create_all` cho test DB nếu đang dựa gián tiếp vào import `app.main`.
- [ ] Sửa `tests/conftest.py:12-14,71-77`: bỏ `TestClient` + `from app.main import app` + fixture `client` (import module-scope → nếu không sửa, xóa `main.py` làm chết **toàn bộ** suite). Làm trong cùng commit với Bước 2.
- [ ] Load-check nhẹ cho `SessionLocal()` per tool call trong `mcp_native` (Q4) — N coordinator đồng thời; chỉnh pool_size nếu cần.

### B1.8 — Chứng minh flow người dùng trước khi chặt cầu (Q3)
- [ ] Chạy thật một phiên coordinator CLI (Claude Code hoặc agy) nối `:8100`: tạo task → dispatch (supervised, human approve qua chat) → review → done, **không chạm REST**. Đây là điều kiện tiên quyết để sang Bước 2.

## Bước 2 — Xóa (một PR, sau khi Bước 1 xanh)

Xóa hẳn:
- [ ] `backend/app/api/` toàn bộ (lưu ý: `ws.py` subscriber đã là dead code — không ai publish `TASK_EVENTS_CHANNEL`; `project_rules.py` chưa từng được include vào router).
- [ ] `backend/app/main.py`.
- [ ] `backend/app/mcp_server.py` (stdio forwarder) — chết cùng endpoint `/api/mcp/tools/call` trong `chat.py`.
- [ ] Tests REST: `test_api_*.py` (7 file), `tests/integration/test_api_dispatch.py`, `test_dispatch_flow.py`, `test_full_flow.py`, `test_streaming.py`, `test_mcp_server.py`.
- [ ] `requirements.txt`: bỏ `fastapi`; **giữ `uvicorn`** (mcp_native dùng), bỏ `httpx` nếu sau khi xóa `mcp_server.py` không còn ai dùng.

Viết lại (chuyển từ TestClient sang gọi service trực tiếp qua `db_session`):
- [ ] `test_coordinator.py:539`, `tests/integration/test_chat_context.py`, `test_tool_chat.py` (đang monkeypatch `app.api.chat.CoordinatorService` → patch `CoordinatorService` trực tiếp; `run_turn_programmatic` là entry point còn sống).
- [ ] `test_agent_matcher.py`, `test_context_generator.py`, `test_dispatch_with_context.py`, `test_token_telemetry.py`.

Ghi chú: `CoordinatorService` KHÔNG chết theo `chat.py` — đường `run_turn_programmatic` (worker wake, CTV2-133, `agent_runner.py:508`) vẫn là driver chính. `stream_turn`/`validate_selection` thành code không ai gọi → dọn ở Bước 3.

## Bước 3 — Dọn sau

- [ ] Xóa schemas mồ côi: `schemas/stats.py`, `events.py`, `audit.py`, `knowledge.py`, `session.py`, `project_rule.py` (chỉ router đã xóa dùng). Kiểm tra `task.py`/`agent.py`/`project.py` với services trước khi đụng.
- [ ] Dọn method chết trong `coordinator.py` (`stream_turn`, `validate_selection`, `completed_turn`).
- [ ] Cập nhật `CLAUDE.md` (bỏ sơ đồ FE/REST, lệnh npm), `README.md`, `docs/coordinator-rules.md`.
- [ ] Xử lý alembic `032_add_project_rules.py` + `schemas/project_rule.py` + `api/project_rules.py` (feature chưa wire — quyết giữ dạng tool MCC hay bỏ).

## Definition of Done

1. `grep -r fastapi backend/app` = 0 kết quả; `pytest backend/tests/` xanh toàn bộ.
2. Một phiên coordinator CLI đi trọn todo→done qua `:8100`, gồm supervised approve và đọc output run bằng `get_run_output`.
3. `docker-compose up` chỉ dựng `db + redis + mcp + worker`, không còn uvicorn `app.main`.

## Giữ nguyên — không đụng

`mcp_native.py`, `mcp_native_issue_token.py`, toàn bộ `services/` (trừ block ws trong coordinator), toàn bộ `workers/` (kể cả `output_streamer.py` — vẫn là publisher + cơ chế cancel-key mà `command_router.py:852-886` và `agent_runner.py:1358` dùng), `db/`, `graph/`, `alembic/`, `scripts/issue-coordinator-token.sh`.
