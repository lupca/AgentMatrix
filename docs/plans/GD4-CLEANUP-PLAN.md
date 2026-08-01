# GĐ4 — Dọn dẹp & kiện toàn chuẩn prod

> 2026-08-01. Phạm vi: chốt B1.8, xóa hẳn lớp FastAPI (Bước 2 của `MCP_MIGRATION_PLAN`), dọn backlog, và đưa cây thư mục về chuẩn prod. KHÔNG đóng gói pip/pipx — mục tiêu là clone về, `.env` + `./scripts/start-backend.sh` là chạy.
> Làm theo thứ tự P0 → P3; mỗi P một PR riêng để review được.

## P0 — Chốt B1.8 bằng một vòng đời SẠCH (điều kiện mở P1)

Vòng done duy nhất đến giờ là vòng agy vượt rào (xem `REVIEW-GD3-B1.md` vòng 3). Cần một vòng sạch với guardrail mới:

- [ ] `./scripts/init-coordinator-workdir.sh ~/ct-coordinator 28800` → chạy coordinator từ đó (KHÔNG phải trong repo này).
- [ ] Đi trọn P1 của `B18-TEST-SCRIPT.md` trên một project/repo test thật: create → spec plan → dispatch supervised → human approve → execute → review four-eyes → verdict → done.
- [ ] Điều kiện đạt: 0 lần Bash-vào-DB/sửa source (soi transcript), `approved_by` trong GateRecord phản ánh human, và nếu coordinator vấp lỗi hệ thống → nó BÁO chứ không tự vá (đây là phép thử guardrail).
- [ ] Nhân tiện chạy P0.4 (SQL count) và P2 (đường lỗi) của kịch bản test.

## P1 — Bước 2: xóa hẳn lớp FastAPI (một PR)

Chi tiết gốc trong `MCP_MIGRATION_PLAN.md` Bước 2; checklist thi hành:

- [ ] Xóa `backend/app/api/` toàn bộ, `backend/app/main.py`, `backend/app/mcp_server.py` (stdio forwarder cũ).
- [ ] Xóa tests REST: 7 file `test_api_*.py`, `tests/integration/test_api_dispatch.py`, `test_dispatch_flow.py`, `test_full_flow.py`, `test_streaming.py`, `test_mcp_server.py`.
- [ ] Viết lại (TestClient → gọi service qua `db_session`): `test_coordinator.py`, `tests/integration/test_chat_context.py`, `test_tool_chat.py`, `test_agent_matcher.py`, `test_context_generator.py`, `test_dispatch_with_context.py`, `test_token_telemetry.py`. (16 test đang fail sẵn nằm trọn trong nhóm này.)
- [ ] `requirements.txt`: bỏ `fastapi`; giữ `uvicorn`; bỏ `httpx` nếu không còn ai dùng sau khi xóa `mcp_server.py`.
- [ ] DoD: `grep -r fastapi backend/app` = 0; `pytest backend/tests -q` xanh 100% (không còn nhóm fail-sẵn); flow B1.8 chạy lại vẫn ok.

## P2 — Bước 3 + backlog tồn đọng

### Code chết & mồ côi
- [ ] Schemas mồ côi (chỉ router đã xóa dùng): `schemas/stats.py`, `events.py`, `audit.py`, `knowledge.py`, `session.py` — kiểm tra import trước khi xóa; `task.py`/`agent.py`/`project.py` GIỮ (services dùng).
- [ ] `coordinator.py`: xóa `stream_turn`, `validate_selection`, `completed_turn` (chỉ chat SSE cũ gọi).
- [ ] `services/tool_definitions.py` (vết tích), `services/llm.py` (shim) — xóa nếu hết caller.
- [ ] Quyết ProjectRule: hoàn thiện tool theo `CONTEXT_RULES_IMPLEMENTATION_PLAN.md` HOẶC gỡ migration 032 + model + api file — không để nửa vời.

### Backlog từ vụ agy (B18-TEST-SCRIPT mục Ghi nhận)
- [ ] `update_task` nhận `mode` trong patch (supervised→bypass qua gate admin, không cho tự do).
- [ ] Điều tra bug result_ref đường bypass — diff tham khảo: `docs/agy-incident-2026-08-01.patch`; fix + test chính chủ.
- [ ] Đối chiếu ReviewResult schema với artifact reviewer CLI viết thật; nếu mismatch → sửa prompt reviewer (ưu tiên) hoặc schema có chủ đích + test. KHÔNG nới strict/extra.

### Mục nhắc vòng 5 (REVIEW-GD3-B1)
- [ ] `docker-compose.yml`: thêm `DATABASE_URL_READONLY` cho service `mcp` (+ bootstrap role trong entrypoint hoặc README).
- [ ] Description `query_db`: thêm `changes-requested` vào enum tasks.status.
- [ ] Tham số hóa password `ct_readonly_user` (env `CT_READONLY_PASSWORD`, default `readonly` cho dev).
- [ ] `statement_timeout` + row cap đưa vào `SETTINGS_WHITELIST` (`sql_timeout_seconds`, `sql_row_cap`).

### Môi trường
- [ ] **Hợp nhất venv**: hiện có 3 (root `.venv` — pip hỏng trỏ project cũ, root `venv/`, `backend/venv`). Giữ MỘT: `backend/venv` (scripts đang dùng). Xóa hai cái kia; ghi vào README "test chạy bằng `backend/venv/bin/python -m pytest`".
- [ ] `conftest`/CI đảm bảo `pglast` có trong venv chuẩn.

## P3 — Dọn root + docs chuẩn prod

### Root: chỉ còn thứ thuộc về sản phẩm
Đích đến — root sau khi dọn:
```
.env.example  .gitignore  CLAUDE.md  README.md  docker-compose.yml  backend/  docs/  scripts/
```
- [ ] Di chuyển tài liệu rời ở root vào docs (giữ git history bằng `git mv`):
  - `MCP_MIGRATION_PLAN.md`, `MCP_ATTACH_PLAN.md`, `QUERY_DB_V2_PLAN.md` → `docs/plans/`
  - `B18-TEST-SCRIPT.md` → `docs/testing/`
  - `REVIEW-GD3-B1.md` → `docs/reviews/`
- [ ] `git rm demo.md` (sản phẩm vòng agy bẩn) và `git rm --cached .worker.pid` (đang bị track dù có gitignore).
- [ ] `.gitignore` bổ sung: `.worker.pid`, `.ct/`, `.codex/`, `.fuse_hidden*`, `.ruff_cache/`, `test-results/` (đã có: worker.log, backend.log, .backend.pid, .agents, venv...).
- [ ] Xóa rác không track: `.fuse_hidden*`, `test-results/`, `.ct/`, root `.agents/`, `.codex/` (artifact các lần test coordinator ngồi nhầm chỗ).

### docs/: mỗi thư mục một vai
- [ ] Cấu trúc đích:
  ```
  docs/
  ├── adr/            # quyết định kiến trúc (giữ nguyên)
  ├── design/         # thiết kế còn hiệu lực
  ├── plans/          # plan đang/đã thi hành
  ├── reviews/        # review + incident (chuyển agy-incident-*.patch vào đây)
  ├── testing/        # kịch bản test
  ├── research/       # giữ nguyên
  ├── archive/        # mọi thứ hết hiệu lực chuyển vào đây, không xóa
  └── coordinator-rules.md   # nguồn sinh instruction files — giữ ở gốc docs
  ```
- [ ] Chuyển vào `docs/archive/`: `frontend-strategy.md`, `mobile-ui-plan.md` (FE đã khai tử), mọi design/plan đã hoàn thành hoặc hết hiệu lực (rà từng file trong `design/`, `plans/`).
- [ ] Mỗi thư mục docs con có `README.md` 2-3 dòng nói vai trò (để coordinator/agent tra cứu không lạc).

### README.md — viết lại thành cửa ngõ "clone về chạy lên"
- [ ] Nội dung: kiến trúc 1 sơ đồ (MCP server + worker + db/redis + coordinator CLIs); Quickstart đúng 5 bước:
  ```
  git clone ... && cd agenticmatix
  cp .env.example .env   # điền MCP_TOKEN_SECRET
  ./scripts/start-backend.sh          # db+redis (docker) + mcp + worker
  ./scripts/create-readonly-role.sh   # role SQL readonly
  ./scripts/init-coordinator-workdir.sh ~/ct-coordinator && cd ~/ct-coordinator && claude|agy|codex
  ```
  cùng bảng scripts, link vào docs/. Bỏ mọi nhắc tới frontend/REST.
- [ ] `CLAUDE.md`: cập nhật kiến trúc (bỏ sơ đồ FE/REST, bỏ lệnh npm, thêm mcp_native/tool surface/coordinator workspace), giữ ngắn.

## DoD toàn GĐ4

1. Root đúng danh sách đích ở P3; `git status` sạch sau một vòng chạy hệ thống (không artifact nào bị track).
2. `pytest backend/tests -q` xanh 100%, chạy bằng venv chuẩn duy nhất.
3. Máy mới: clone → 5 bước Quickstart → coordinator điều phối được một task end-to-end.
4. `grep -ri fastapi backend/app docs/README* CLAUDE.md` = 0 (trừ archive).
