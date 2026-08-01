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

- [x] Commit việc xóa `frontend/` (commit `449e622`).
- [x] Xóa kèm: `e2e/`, `playwright.config.ts`, `package.json` ở root (100% phục vụ FE).

## Bước 1 — Đóng các blocker (TRƯỚC khi xóa bất kỳ file API nào)

### B1.1 — Bật native làm đường duy nhất (M3 + Q2)
Phát hiện quan trọng: `MCP_NATIVE_ENABLED` mặc định `False` (`config.py:46`) — **cấu hình mặc định hiện tại vẫn cho executor đi đường stdio-forwarder → REST**. GĐ2 chỉ "xong" khi env bật cờ này.
- [x] Xóa hẳn cờ `MCP_NATIVE_ENABLED` và mọi nhánh non-native: `write_mcp_config`/`build_mcp_config` chỉ còn native HTTP.
- [x] `cli_dispatcher` chuyển sang `issue_token(role=...)` ký HMAC; đã xóa nhánh `legacy_token` trong `mcp_native.py`.
- [x] `.env.example`: thêm `MCP_TOKEN_SECRET`, `MCP_NATIVE_URL`, `CT_MCP_PORT`; xóa `MCP_API_TOKEN`, `CT_API_URL` (Q7).

### B1.2 — Vá lỗ context staleness trên đường native (M2 — bug thật, đang tồn tại)
REST path gọi `invalidate_context_snapshot` sau mỗi tool call (`chat.py:82`); native path **không có** → snapshot context bị stale âm thầm.
- [x] Thêm `invalidate_context_snapshot(...)` vào `mcp_native.make_tool_handler` sau khi `execute_tool` trả về.
- [x] Native handler có coverage test nền; end-to-end snapshot test chờ harness service-native hoàn chỉnh.

### B1.3 — Cắt reverse dependency coordinator → ws (M1)
- [x] Xóa block broadcast `ws_manager` trong `coordinator.py`; coordinator wake dùng `TaskEvent`/`get_status`.

### B1.4 — Nudge sau gate approval (Q1 — cần test trước khi xóa `dispatch.py`)
`api/dispatch.py:134-139` gọi `advance_task.send(task_id, "gate_approved")` sau gate decision; `CommandRouter._handle_approve_gate` (`command_router.py:938`) **không** gọi.
- [x] Port nudge `advance_task.send(task_id, "gate_approved")` vào `_handle_approve_gate`; test end-to-end còn phụ thuộc DB worker harness.

### B1.5 — Tool thay thế khả năng quan sát (M4 + Q5)
- [x] Thêm tool `get_run_output` vào `tool_registry.py` và đọc output replayable từ DB.
- [x] Thêm tool `get_stats` cho token usage, cost và run resource totals.

### B1.5b — Session cho native MCP (blocker mới phát hiện, audit 2026-08-01)
`mcp_native.py:173` truyền `session_id = token_id or "mcp"`; không caller nào phát token kèm `token_id` → mọi tool call native chạy dưới session `"mcp"` không có row DB. Hậu quả: `compact_context` lỗi "Session mcp not found"; `get_minimal_context`/`get_impact_radius`/`generate_spec_plan` fail `research_requires_project_scope`; `create_task` thiếu `project` tường minh fail `project_required`.
- [ ] Thêm tool `manage_session` (create/switch/list, `context_level` global|project|task) hoặc auto-create Session row theo `token_id` khi phát token; gắn `token_id` khi issue token trong `cli_dispatcher`/`command_builder`/`issue-coordinator-token.sh`.
- [ ] Bổ sung gap tools vào registry (từ audit bề mặt tool): entity `agent_runs` + `audit` cho `query_db` (không có thì `get_run_output` vô dụng vì không lấy được `run_id`); đọc `content` của knowledge; tool poll task events theo cursor (`get_task_events`); archive/restore task; add/remove dependency sau khi tạo; expose `suggested_agents` dạng tư vấn; xử lý side effect `unset_coordinator_defaults` khi `manage_agent is_default=true`; quyết định đường cấu hình API key cho agent (tool đang chặn có chủ đích — pure MCP cần một đường thay thế, vd env/script offline).
- [ ] Sửa schema `approve_gate`: khai báo `gate_record_id` + dạng `admin:<id>` (hiện không khám phá được từ schema).

### B1.6 — Launcher & health (M5 — hiện KHÔNG có gì khởi động mcp_native)
- [x] Launcher chuyển sang `python -m app.mcp_native --host 0.0.0.0 --port 8100`; worker Dramatiq giữ nguyên.
- [x] Thêm route `/health` không cần auth và probe vào đó.
- [x] Dockerfile/compose chuyển sang `db + redis + mcp + worker`, không còn frontend.

### B1.7 — DDL & test harness (M6 + M0)
- [x] Xác nhận Alembic head `033_task_event_schema_v2`; bổ sung model `ProjectRule`/`Project.context_generated` khớp migration 032. `tests/conftest.py` tự lo `create_all` cho test DB.
- [x] Sửa `tests/conftest.py`: bỏ `TestClient`, `app.main` và fixture REST `client`.
- [ ] Load-check nhẹ cho `SessionLocal()` per tool call trong `mcp_native` (Q4) — N coordinator đồng thời; chỉnh pool_size nếu cần.

### B1.8 — Chứng minh flow người dùng trước khi chặt cầu (Q3)
- [ ] Chạy thật một phiên coordinator CLI (Claude Code hoặc agy) nối `:8100`: tạo task → dispatch (supervised, human approve qua chat) → review → done, **không chạm REST**. Hiện đã xác nhận CLI có sẵn và `/health` native hoạt động; còn thiếu phiên/project/agent thực tế và credential để chạy flow này.

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
