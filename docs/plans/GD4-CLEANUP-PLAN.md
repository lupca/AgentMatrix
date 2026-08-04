# GĐ4 — Dọn dẹp & kiện toàn chuẩn prod

> 2026-08-01. Phạm vi: chốt B1.8, xóa hẳn lớp FastAPI (Bước 2 của `MCP_MIGRATION_PLAN`), dọn backlog, và đưa cây thư mục về chuẩn prod. KHÔNG đóng gói pip/pipx — mục tiêu là clone về, `.env` + `./scripts/start-backend.sh` là chạy.
> Làm theo thứ tự P0 → P3; mỗi P một PR riêng để review được.

## P0 — Chốt B1.8 bằng một vòng đời SẠCH (điều kiện mở P1)

Vòng done duy nhất đến giờ là vòng agy vượt rào (xem `REVIEW-GD3-B1.md` vòng 3). Cần một vòng sạch với guardrail mới:

- [x] `./scripts/init-coordinator-workdir.sh ~/ct-coordinator 28800` → chạy coordinator từ đó (KHÔNG phải trong repo này).
- [x] Đi trọn P1 của `B18-TEST-SCRIPT.md` trên project/repo test thật (ct-demo): CTDE-001, CTDE-003, CTDE-011 đều todo→done trọn vòng 2026-08-01, four-eyes giữ, verdict từ review run thật. (Phải sửa 4 bug chặn đường trước — xem mục "Kết quả test B1.8" dưới.)
- [x] Guardrail đạt: coordinator báo `failed` nguyên văn, thử approve bị từ chối thì dừng đề xuất, không Bash-vào-DB (một lần curl thẳng endpoint MCP — vẫn qua auth + gate, chấp nhận được).
- [x] Cảnh cuối: dispatch **supervised** với human approve — hoàn thành 2026-08-01 chiều bằng CTV2-216 (todo→done trọn vòng, human approve cả 3 gate dispatch/review_order/verdict, four-eyes sonnet-high/sonnet-low, suite 499 xanh). Phải sửa 2 bug chặn đường: flush trước CAS (ck_tasks_terminal_not_awaiting_approval) và attempt slot cho re-review (ccaf39f, 215f9fc).

## P1 — Bước 2: xóa hẳn lớp FastAPI (một PR)

Chi tiết gốc trong `MCP_MIGRATION_PLAN.md` Bước 2; checklist thi hành:

- [ ] Xóa `backend/app/api/` toàn bộ, `backend/app/main.py`, `backend/app/mcp_server.py` (stdio forwarder cũ).
- [ ] Xóa tests REST: 7 file `test_api_*.py`, `tests/integration/test_api_dispatch.py`, `test_dispatch_flow.py`, `test_full_flow.py`, `test_streaming.py`, `test_mcp_server.py`.
- [ ] Viết lại (TestClient → gọi service qua `db_session`): `test_coordinator.py`, `tests/integration/test_chat_context.py`, `test_tool_chat.py`, `test_agent_matcher.py`, `test_context_generator.py`, `test_dispatch_with_context.py`, `test_token_telemetry.py`. (16 test đang fail sẵn nằm trọn trong nhóm này.)
- [ ] `requirements.txt`: bỏ `fastapi`; giữ `uvicorn`; bỏ `httpx` nếu không còn ai dùng sau khi xóa `mcp_server.py`.
- [ ] DoD: `grep -r fastapi backend/app` = 0; `pytest backend/tests -q` xanh 100% (không còn nhóm fail-sẵn); flow B1.8 chạy lại vẫn ok.

## Kết quả test B1.8 (2026-08-01) — nguồn: `B18-TEST-SCRIPT.md` mục Ghi nhận

Chi tiết từng vấn đề ở dạng task file tại `~/projects/control-tower/projects/agenticmatix/tasks/CTV2-211..226`.

**Đã sửa trong lúc test (dev review lại + bổ sung test):**
- CTV2-211: agy nuốt prompt khi flag chen giữa `--print` và prompt → sửa thứ tự argv trong `command_builder.py`.
- CTV2-212 (critical): run_agent bị giao 2 lần — 3 call site sync trong `command_router` không ghi `dramatiq_message_id` → outbox publisher gửi bản sao. Đã ghi message_id + commit sau send.
- CTV2-213 (critical): ReviewResult schema tự mâu thuẫn prompt (`verdict` bị `extra="forbid"` chặn; tests_* không nói là mảng) → `verdict` thành field thật + prompt nói rõ kiểu.
- CTV2-214 (critical): verdict bị từ chối oan do `autoflush=False` → `db.flush()` trước `_submit_review_verdict`.
- CTV2-215: tool mới `wait_for_task` (long-poll, trả trọn gói task+run+events) — thay polling timer, đã e2e verify.

**Cần dev fix (theo thứ tự ưu tiên đề nghị):**
1. CTV2-216 (high): reaper cho run `running` có PID chết — đang chiếm slot concurrency vĩnh viễn.
2. CTV2-217 (high): run_agent dead-letter → run kẹt `queued` mãi, cần handler mark failed.
3. CTV2-219 (high): vệ sinh retry — lock per-run, idempotency key theo attempt, xóa branch `ct-run/*` cùng worktree, fallback shared-tree thành lỗi cứng, hủy run khi task kết thúc.
4. CTV2-220 (high): agy headless làm việc trong scratch dir thay vì cwd — research workspace mechanism; tạm cấm agy làm executor; chuẩn hóa model/effort mapping agy.
5. CTV2-221 (high): escalation set `awaiting_approval` không kèm GateRecord → ngõ cụt approve + chặn dispatch lại.
6. CTV2-218 (med): dedupe chuỗi self-reschedule outbox/reconcile (166×2 chuỗi, log storm).
7. CTV2-222 (med): `default_mode` là nút chết — nối vào thật hoặc bỏ whitelist; docs cho `autonomy`.
8. CTV2-223 (med): reviewer pool dính id rác (`@user`, `@lupca`) — rà bảng agents + filter.
9. CTV2-224 (med): `update_task` nhận `mode` qua gate.
10. CTV2-225 (med): tái hiện result_ref đường bypass sau các fix trên; còn thì điều tra tiếp.
11. CTV2-226 (med): gỡ `wake_coordinator` dead path (gộp vào P1 xóa FastAPI/coordinator cũ).

**Phát hiện thêm 2026-08-01 (chiều, sau P1):**
- CTV2-228 (DONE 3690029): gốc là mapping chỉ đọc `executor`/`reviewer` còn caller truyền `agent_id` → bị vứt lặng lẽ, matcher chiếm quyền. Mapping giờ nhận alias `agent_id`; verify sống payload gate ghi đúng agent chỉ định.
- CTV2-229 (done): migrate_md_to_db từng clear `agents` (mất api_key qua CASCADE agent_accounts, reset điểm đo) và reset `next_task_seq` (create_task sinh id trùng) — đã sửa: agents upsert giữ key/điểm, re-seed counter sau import.
- Reminder `pending_approvals` từng dính task đã archive (đã sửa: lọc `archived_at IS NULL` cả hai ledger + escalation).
- CTV2-227 (đang chạy qua dispatch): nối lại flow Project Context & Rules — tool `save_project_context` + inject context/rules vào prompt (test cũ là test rỗng, chưa từng có injection).
- CTV2-216 (DONE qua dispatch): reaper cho run running PID chết — merge 4084fb7.
- CTV2-230 (med): driver và request_review mỗi bên tạo một gate review_order → gate mồ côi ám reminder (đã reject 2 gate trùng bằng child-row system:cleanup).
- CTV2-231 (high): review run bị watchdog cancel ("no progress") để task kẹt in-review — cancel phải đi qua record_review_failure như failure; đã phải phẫu thuật SQL gỡ CTV2-227.
- Đã sửa tại chỗ (kèm test xanh 499): `_cas_status` flush ORM trước raw UPDATE (verdict supervised không land được done); review re-order lấy max(attempt)+1 (đụng uq_agent_runs_round_kind_attempt).
- CTV2-217 (DONE qua dispatch): dead-letter callback `on_retry_exhausted` — merge 08aa3a4, suite 503.
- CTV2-232 (high): watchdog no-progress giết oan CLI im lặng (`claude -p` không stream) — tạm nới `max_no_progress_seconds=2400` (admin:15); fix chuẩn = heartbeat theo PID sống.
- CTV2-233 (critical, ĐÃ SỬA ac34bf0): schema `approve_gate` không có trường `decision` → mọi human REJECT qua MCP bị ghi thành approve (quan sát live: reject verdict rubber-stamp làm task done). Dev cần: test e2e reject + rà các tool khác cùng pattern mapping-vứt-arg.
- CTV2-238 (DONE d3ff4ab): landing — verdict pass tự merge vào main, done kèm landed_ref; conflict escalate; tool land_task retry/backfill.
- CTV2-234 (DONE 3690029): re-dispatch chính thống từ changes-requested/failed; constraint terminal nới còn done (migration 037).
- Bằng chứng mới cho CTV2-220/223: agy reviewer rubber-stamp CTV2-227 vòng 2 (pass 4/4, 0 findings trong khi F1 HIGH còn nguyên) — agy không đáng tin cả vai reviewer, matcher vẫn tự chọn nó (CTV2-228).
- CTV2-227 (DONE, 4 vòng: 3 dispatch + 1 human replan sau khi auto_max_rounds 3/3 escalate đúng thiết kế): flow Project Context & Rules THÔNG end-to-end — tool `save_project_context` (executor-scoped, chặn cross-project write), inject [Project Context]+[Project Rules] vào dispatch/review prompt, đã demo thật: agent quét repo → gọi tool → context+4 rules vào DB → dispatch mới tự inject (merge 3f46d3c, 765c165; suite 513).
- CTV2-235 (DONE 3690029): tag `no-commit` + `RESULT_REF: none` (worktree sạch) → done qua gate verdict hệ thống; khai none mà có commit vẫn fail.
- Fix thêm: glob `**/` không match con trực tiếp (fnmatch) → rules không bao giờ inject; đã sửa + test (765c165).

**Phát hiện tối 2026-08-01 (từ phiên coordinator voma-invoice, /tmp/convention.txt):**
- CTV2-236 (critical, ĐÃ SỬA b9e7631): `generate_spec_plan` chết cả 2 đường — CLI không truyền `--effort` (agy exit 1 với gemini-3.6-flash vốn bắt buộc effort); API không parse được reasoning model (`<think>…</think>{json}` chết char 0, max_tokens 1200 quá bé). Verify sống: VOMA-001 sinh plan ok bằng cả agy lẫn DeepSeek/SiliconFlow.
- CTV2-237 (critical, ĐÃ SỬA 57641e0): admin gate update dạng `{id, patch}` là no-op lặng lẽ — whitelist lọc key phẳng, patch lồng bị vứt sạch, approve xong không đổi gì; giờ unwrap + reject update rỗng.
- ct_readonly_user thiếu SELECT trên bảng mới (query_db "permission denied for table agents") — đã GRANT + default privileges; cần đưa vào `create-readonly-role.sh`.

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

## Ghi chú CTV2-1378 (2026-08-05)

Đã tách `generate_spec_plan` nội bộ thành plan→DB→critic (xem `docs/spec/03-gates-and-autonomy.md`).
Đường inline cũ (hai `await` LLM trong cùng một giao dịch) đã bị bỏ hẳn — không
còn giữ lại song song. Việc CÒN LẠI, chưa làm ở đây: đưa cả hai bước (plan +
critic) qua Dramatiq worker để có pid/retry/hủy như AgentRun thật — theo dõi ở
CTV2-1380.
