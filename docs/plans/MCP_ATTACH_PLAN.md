# Plan: MCP attachment tại spawn-time, per-CLI adapter

> 2026-08-01. Bổ sung cho `MCP_MIGRATION_PLAN.md` (chèn giữa B1.7 và B1.8 — B1.8 cần cái này để executor/coordinator codex + agy có tool).
> Bối cảnh: xác nhận `--mcp-config` chỉ là cơ chế của claude. Codex nhận MCP qua cờ `-c mcp_servers.*` / `config.toml`; agy đọc file cố định `.agents/mcp_config.json` và **bắt buộc dùng `serverUrl`** (không phải `url`). Thay đổi dở dang hiện tại trên `cli_dispatcher`/`command_builder` (bỏ hẳn MCP cho codex/agy) đúng ở vế "gỡ flag sai" nhưng chưa được dừng ở đó — executor/coordinator codex + agy đang mất toàn bộ CT tools.

## Thiết kế

Nguyên tắc: **builder không đụng MCP nữa; worker gắn MCP tại thời điểm spawn thật.**

```
request gate (task_orchestration)          spawn (agent_runner / cli_dispatcher)
  build command KHÔNG flag MCP      →        issue_token(ttl = run timeout)
  payload: {command, cli, timeout,           attach_mcp(cli, ...) → command cuối
            agent_role, ...}                 + env + cleanup paths
```

Lợi ích kéo theo (sửa được 3 vấn đề cũ cùng lúc):
1. Token phát tại spawn → TTL = đúng run timeout, **bỏ margin 30 phút** vá chuyện queue ăn TTL (commit `5c67671` — gỡ `_MCP_TTL_QUEUE_MARGIN_SECONDS`).
2. Command trong gate payload không còn đường temp file ngẫu nhiên → **ổn định cho idempotency hash**.
3. Một chỗ duy nhất (`attach_mcp`) hiểu đặc thù từng CLI — thêm CLI mới chỉ thêm một nhánh.

## Adapter per-CLI

| CLI | Cơ chế | Token đi đường nào | Cleanup |
|---|---|---|---|
| claude | append `--mcp-config <temp.json>` vào argv | trong file JSON, chmod 0600 | `finally` của `run_agent` (cơ chế `_cleanup_mcp_config` sẵn có) / `finally` của `spawn` |
| codex | append `-c mcp_servers.control-tower.url=<MCP_NATIVE_URL>` + `-c mcp_servers.control-tower.bearer_token_env_var=CT_MCP_TOKEN` | **env var `CT_MCP_TOKEN`** qua ProcessManager | không có file; env chết theo process |
| agy | ghi `<workdir>/.agents/mcp_config.json` — schema `{"mcpServers": {"control-tower": {"serverUrl": ..., "headers": {"Authorization": "Bearer <token>"}}}}` | trong file, chmod 0600 | run có worktree: tự biến mất khi worktree bị gỡ; không worktree (coordinator): xóa ở `finally` |

### Ràng buộc bảo mật (không thương lượng)

- **Token không bao giờ nằm trong argv.** Argv lộ qua `ps`/process listing và chuỗi command được lưu vào `AgentRun.command` trong DB. Vì vậy codex bắt buộc đi đường `bearer_token_env_var` + env thật của process — KHÔNG dùng kiểu `CT_MCP_TOKEN=xxx codex ...` trong shell string (chạy qua `sh -c` thì vẫn hiện nguyên trong ps).
- **`.agents/` phải nằm trong git exclude của worktree** (ghi vào `<worktree>/.git/info/exclude` khi tạo worktree) để executor không commit file chứa token; reviewer read-only git env không bị ảnh hưởng.
- File config chmod `0600` như hiện tại.

## Việc cụ thể

### 1. `process_manager.py`
- [ ] `run_with_streaming(command, cwd, env: dict[str, str] | None = None)` — merge `env` vào `os.environ` copy khi spawn. Không log giá trị env.

### 2. `agent_runner.py` (executor path)
- [ ] Hàm `attach_mcp(cli, command, workdir, task_id, role, timeout_seconds) -> (command, env, cleanup_paths)`:
  - phát token `issue_token(role=role, task_id=task_id, ttl_seconds=timeout_seconds + nhỏ~120s grace)` — phát **tại đây**, không nhận token từ payload;
  - nhánh claude/codex/agy như bảng trên; workdir = worktree path nếu có, ngược lại repo_root.
- [ ] Gọi trong `run_agent` sau khi worktree sẵn sàng, trước khi spawn; dọn `cleanup_paths` trong `finally` (mở rộng `_cleanup_mcp_config` hiện có thành nhận danh sách path thay vì parse argv).
- [ ] Ghi `.agents` vào `<worktree>/.git/info/exclude` trong `WorktreeManager` (hoặc ngay trong `attach_mcp`).

### 3. `command_builder.py`
- [ ] Gỡ `_native_mcp_config` + tham số `mcp_ttl_seconds` + mọi nhánh `--mcp-config`/`-c mcp_servers` khỏi `build_dispatch_command`/`build_review_command` — builder chỉ build lệnh CLI thuần.
- [ ] `task_orchestration`: bỏ `mcp_ttl_seconds=` ở 2 call site (payload `timeout_seconds` đã có sẵn cho worker dùng).

### 4. `cli_dispatcher.py` (coordinator path)
- [ ] `spawn` dùng chung `attach_mcp` (import từ nơi trung lập — cân nhắc đặt `attach_mcp` trong module riêng `app/services/mcp_attach.py` để tránh vòng import worker↔service), role="coordinator", workdir = `self.working_directory`, ttl mặc định coordinator (1h).
- [ ] Khôi phục MCP cho coordinator codex + agy (hiện đang bị bỏ trống trong thay đổi dở dang).

### 5. `mcp_native.py`
- [ ] Không đổi gì — `X-CT-Role` header chỉ là trang trí (role thật nằm trong token), giữ hay bỏ tùy dev.

### 6. Tests
- [ ] `test_mcp_attach.py` mới:
  - claude: argv chứa `--mcp-config`, file JSON đúng schema `url`, token trong file không trong argv;
  - codex: argv chứa 2 cờ `-c` đúng key, **assert token KHÔNG xuất hiện trong command string**, env trả về có `CT_MCP_TOKEN`;
  - agy: file `.agents/mcp_config.json` dùng **`serverUrl`** (assert không có key `url`), headers có Bearer; argv không có flag MCP;
  - cleanup: mọi path trả về bị xóa sau `finally`.
- [ ] Sửa `test_native_phase2_wiring.py::test_executor_command_gets_task_scoped_native_token` theo thiết kế mới: builder KHÔNG còn tạo config (assert command sạch flag MCP); thêm test wiring ở tầng `run_agent`/`attach_mcp` thay thế.
- [ ] `test_cli_coordinator.py`: codex/agy coordinator có MCP trở lại.

### 7. Dọn
- [ ] Gỡ `_MCP_TTL_QUEUE_MARGIN_SECONDS` (lý do tồn tại đã hết).
- [ ] Cập nhật `MCP_MIGRATION_PLAN.md` (đánh dấu mục này) và `docs/coordinator-rules.md` nếu có nhắc cơ chế config.

## Thứ tự & phối hợp

1. Việc này **đè lên đúng các file dev đang sửa dở** (`cli_dispatcher`, `command_builder`, `test_cli_coordinator`) — người làm nên là người đang cầm working tree đó, hoặc commit/stash phần dở trước khi người khác vào.
2. Làm xong mục này thì `test_native_phase2_wiring` hết fail, và B1.8 chạy được với **cả ba** CLI thay vì chỉ claude.
3. Sau B1.8 xanh → Bước 2 (xóa `app/api/`) theo `MCP_MIGRATION_PLAN.md` như cũ.

## Definition of Done

- Cả 3 CLI (executor lẫn coordinator) gọi được CT tools qua native MCP; test chứng minh token không lộ trong argv/`AgentRun.command`; toàn bộ suite liên quan xanh; command trong gate payload deterministic (không còn temp path).
