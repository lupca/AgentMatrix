# Review GĐ3 Bước 0–B1 (commits 449e622, d74121e, d9c86d8, faff559)

## VÒNG 3 — review MCP_ATTACH_PLAN implementation (2026-08-01) — **ĐÃ FIX TOÀN BỘ**

Trạng thái sau fix: #1 role coordinator truyền đúng + bỏ task scope ảo, có test decode token assert role/task_id cho cả hai đường và test dispatcher-level; #2 `_ensure_git_exclude` dùng `git rev-parse --git-path info/exclude` (trỏ đúng common dir), có test worktree git thật assert `git status` sạch; #3 agy merge key `control-tower` vào config có sẵn + backup `.ct-orig`, `detach_mcp` restore nguyên văn thay vì xóa (backup cả khi JSON gốc hỏng), có test merge/restore; #4 emit `TaskEvent mcp_attach_failed` khi attach fail; #5 log warning khi `run.cli` rỗng. Cleanup hai đường đều đi qua `detach_mcp`. 237 tests pass.

Đã xác minh ĐÚNG: `ProcessManager` nhận `env` và merge sạch sẽ; codex token đi env `CT_MCP_TOKEN`, **không xuất hiện trong argv** (đúng ràng buộc số 1); agy dùng `serverUrl`; claude giữ nguyên hành vi; cleanup theo danh sách path trong `finally` cả hai đường; command builder giờ deterministic; `_ensure_git_exclude` xử lý cả gitdir-file của worktree. 81 test pass.

### 1. [CRITICAL] Coordinator nhận token EXECUTOR
`cli_dispatcher.spawn` gọi `attach_mcp(..., task_id="coordinator", timeout_seconds=3600, ...)` **không truyền `role`** → default `role="executor"`. Hậu quả: coordinator cầm token executor bị scope vào task ảo `"coordinator"` — mọi tool coordinator-only (`create_task`, `dispatch_task`, `approve_gate`, `record_verdict`...) bị server từ chối "requires a coordinator token", và `_task_scope_ok` bắt `task_id` argument phải bằng `"coordinator"` nên tool executor cũng gần như không gọi được. **Coordinator tê liệt hoàn toàn** — không test nào bắt được vì `test_mcp_attach.py`/`test_cli_coordinator.py` không assert role trong token.
Fix: truyền `role="coordinator"`; đổi default `task_id` của `attach_mcp` thành `None` (token coordinator không nên mang claim task ảo); **thêm test decode token và assert `role`/`task_id`** cho cả hai đường.

### 2. [HIGH] Git exclude cho `.agents` VÔ TÁC DỤNG trong worktree — đúng case executor
Đã kiểm chứng thực nghiệm: ghi `.agents` vào `.git/worktrees/<name>/info/exclude` (chỗ `_ensure_git_exclude` đang ghi khi gitdir là file trỏ đi) → `git status` trong worktree **vẫn thấy `?? .agents/`**. Git chỉ đọc `info/exclude` từ **common dir**. Executor agent giữa run hoàn toàn có thể `git add -A` và commit file chứa token.
Fix: dùng đúng API git — `git rev-parse --git-path info/exclude` chạy trong `workdir` trả về path common dir chính xác (đã verify); hoặc resolve `commondir`. Thêm test tạo worktree thật và assert `git status --porcelain` sạch.

### 3. [MEDIUM] agy coordinator ghi đè rồi XÓA file config có sẵn của người dùng
Đường coordinator, `workdir = self.working_directory` (thư mục thật của người dùng): nếu đã tồn tại `.agents/mcp_config.json` (người dùng khai báo MCP server khác cho agy), `attach_mcp` ghi đè không hỏi, và cleanup `finally` **xóa luôn** — mất config gốc của người dùng.
Fix: nếu file tồn tại → merge key `control-tower` vào JSON hiện có, cleanup chỉ gỡ key đó ra (restore nội dung cũ); hoặc tối thiểu backup/restore.

### 4. [LOW] Fail attach → run chạy tiếp không MCP, chỉ có log
`run_agent` bọc `attach_mcp` trong `except Exception` rồi chạy tiếp bằng command trần. Executor mất CT tools giữa chừng khó debug. Đề xuất: emit thêm `TaskEvent` kind=info để coordinator nhìn thấy, thay vì chỉ log worker.

### 5. [LOW] `run.cli or "claude"` fallback im lặng
Nếu `run.cli` rỗng thì mặc định nhánh claude — nên log warning vì token/config claude gắn cho CLI khác là vô nghĩa.

---

## VÒNG 2 — verify commit 9641536 (2026-08-01)

Đã xác minh sửa đúng: nudge log warning + trả `nudged: true/false` trong response; token luôn có `token_id`+`session_id`+`exp`; `_ensure_session` auto-create Session đúng scope (task cho executor, global cho coordinator); server fail-fast `SystemExit` khi thiếu `MCP_TOKEN_SECRET`. Vấn đề #1, #2, #3 vòng 1: ĐÓNG.

Vấn đề còn lại sau vòng 2:

1. **[MEDIUM] TTL executor 900s trùng khít RUN_TIMEOUT_SECONDS mặc định (900s), không có margin.** Hai lỗ: (a) `run_timeout_seconds` là setting runtime-overridable — operator nâng timeout lên 3600 qua `update_settings` thì token vẫn chết ở phút 15, executor mất MCP giữa chừng; (b) token phát lúc **build command**, nhưng run có thể nằm QUEUE (brake `max_concurrent_runs`) trước khi chạy — thời gian xếp hàng ăn vào TTL. Sửa: `command_builder` truyền `ttl_seconds = resolved run_timeout + margin (vd +600s)`, hoặc phát token lúc spawn thật.
2. **[LOW] TTL coordinator 1h vs phiên tương tác dài.** Phiên Claude Code ngồi làm việc quá 1h sẽ 401 toàn bộ, phải phát token mới + reconnect. Thêm flag `--ttl` cho `scripts/issue-coordinator-token.sh` (con người tự chọn 8–24h); TTL 1h giữ cho token máy phát per-turn.
3. **[LOW] Race trong `_ensure_session`**: hai call đầu tiên cùng token đồng thời → cả hai miss `db.get`, cái sau IntegrityError khi commit. Bọc `IntegrityError` → rollback → re-get.
4. **Lưu ý session coordinator global**: `_ensure_session` tạo session `context_level=global, project_id=None` cho coordinator → các research tool cần project scope có thể vẫn fail nếu không truyền project tường minh. Cần xác nhận trong B1.8 (là một phần lý do B1.8 phải chạy thật).

Về plan: dev báo "B1.5b hoàn tất" nhưng trong plan **2 checkbox của B1.5b vẫn mở đúng** (gap tools registry + schema `approve_gate`) — phần session/token đã xong, phần gap tools chưa. Không đánh dấu B1.5b done cho đến khi xong 2 mục đó.

---

## VÒNG 1 (giữ lại để đối chiếu)

> Review 2026-08-01, trên main chưa push. Kết luận: **hướng làm đúng, được merge tiếp**, nhưng có 2 vấn đề mức trung bình cần sửa trước Bước 2, và 1 điều chỉnh thứ tự: làm B1.5b TRƯỚC B1.8.

## Đã xác minh ĐÚNG

- B1.1: nhánh `legacy_token` và `MCP_NATIVE_ENABLED` xóa sạch; `cli_dispatcher`/`command_builder` đều phát token HMAC ký; `.env.example` đã đổi sang `MCP_TOKEN_SECRET`/`MCP_NATIVE_URL`/`CT_MCP_PORT`.
- B1.2: `invalidate_context_snapshot(db, project_id=None)` trong `mcp_native.make_tool_handler` khớp đúng ngữ nghĩa REST cũ (`chat.py:82` cũng gọi `project_id=None`).
- B1.4: nudge `advance_task.send(task_id, "gate_approved")` đã port vào `_handle_approve_gate`, driver idempotent nên không double-run. (Nhưng xem vấn đề #1.)
- File MCP config tạm KHÔNG leak: đường executor dọn bằng `_cleanup_mcp_config` trong `run_agent` finally; đường coordinator dọn trong `CLIDispatcher.spawn` finally.
- `/health` exempt đúng một path, method GET, không mở rộng bề mặt auth.
- `ProjectRule` + `Project.context_generated` khớp migration 032 — hết nguy cơ ImportError.
- Test: chạy độc lập 5 suite liên quan (`test_mcp_native`, `test_native_phase2_wiring`, `test_cli_coordinator`, `test_tool_registry`, `test_command_router`) → **80 passed**.

## Vấn đề cần sửa

### 1. [MEDIUM] Nudge sau approve nuốt lỗi im lặng
`command_router.py` (`_handle_approve_gate`, khối `advance_task.send` mới):
```python
try:
    advance_task.send(result.task.id, "gate_approved")
except Exception:
    pass
```
Nếu Redis/broker chết đúng lúc approve: gate approved thành công nhưng driver không bao giờ được đánh thức, task đứng im, **không một dòng log**. Sửa: log warning + trả tín hiệu trong response (`"nudged": false`) để coordinator LLM biết mà gọi lại/báo người. Đây đúng loại lỗi mà pattern "tool result dẫn đường" sinh ra để xử lý.

### 2. [MEDIUM] Token không có hạn (TTL)
`issue_token` không có claim `exp` — token coordinator được phát **mỗi lượt spawn**, ghi ra file /tmp, nhưng mỗi token có giá trị **vĩnh viễn** cho đến khi xoay `MCP_TOKEN_SECRET`. File được dọn nhưng token còn nằm trong command line/process listing/log là dùng lại được mãi. Sửa: thêm `exp` vào payload + check trong `authenticate_token`; TTL đề xuất: executor = run timeout của task, coordinator = ngắn (giờ, không phải ngày). Làm trước Bước 2 vì sau đó không còn REST auth nào khác.

### 3. [LOW] Server không fail-fast khi thiếu secret
`MCP_TOKEN_SECRET` mặc định `""` → `python -m app.mcp_native` khởi động bình thường nhưng 401 mọi request (trừ `/health` — health xanh càng gây hiểu lầm "server ổn"). Sửa: refuse start khi secret rỗng, message rõ ràng.

### 4. [LOW] `get_stats` query kém hiệu quả
`usage_query.all()` load toàn bộ row để sum bằng Python; `resources.count()` + iterate chạy cùng query 2 lần. Đúng về kết quả, nhưng nên chuyển sang `func.sum(...)` aggregate — bảng `LLMUsage` sẽ lớn nhanh nhất hệ thống. Sửa lúc nào cũng được.

### 5. Dọn cосметic (gộp vào Bước 3)
- `_attach_mcp_config` giờ là no-op — xóa hàm + call sites.
- `CLIDispatcher.api_url` / tham số `api_url` của `build_mcp_config` chỉ còn là vỏ tương thích (`del api_url`) — xóa khi hết caller.

## Điều chỉnh thứ tự: B1.5b phải làm TRƯỚC B1.8

B1.8 đang chờ "project/agent/credential thực tế", nhưng kể cả có đủ, flow sẽ vấp ngay session gap của B1.5b: token phát ra vẫn không có `token_id` → mọi call chạy dưới session `"mcp"` không có row DB → `create_task` thiếu `project` fail `project_required`, research tools fail scope, `compact_context` lỗi. **B1.5b là điều kiện tiên quyết của B1.8**, không phải mục song song. Đề xuất khi làm B1.5b: gắn luôn `session_id` (hoặc auto-create Session) vào lúc phát token — cùng chỗ với việc thêm `exp` ở vấn đề #2, sửa một lần hai việc.
