# 07 — Vận hành & runbook

## Khởi động / khởi động lại

```bash
./scripts/start-backend.sh      # db+redis (docker) → alembic → mcp_native :8100 → dramatiq worker
# pid files: .backend.pid (server), .worker.pid (worker); log: backend.log, worker.log
```
- Script tự source root `.env` (`set -a`) rồi ép `DATABASE_URL=postgresql://ct:secret@localhost:5433/control_tower`,
  `REDIS_URL=redis://localhost:6380/0`.
- Restart tay: kill pid trong pid-file; nếu phải pkill thì dùng bracket pattern
  (`pkill -f "app.mcp_nativ[e]"`) — pattern trần sẽ match chính shell đang chạy lệnh.
- Sau restart LUÔN verify: `curl -s localhost:8100/health` = 200 và
  `ps` thấy đúng pid mới (server cũ giữ port làm bản mới chết im lặng — đã dính).
- Đổi code services dùng bởi worker (task_orchestration, outbox, command_builder...)
  thì phải restart CẢ worker, không chỉ server.

## Hạ tầng

- Postgres: container **`agmx_db`**, port 5433, user `ct`.
  `docker exec -i agmx_db psql -U ct -d control_tower`.
- Role đọc `ct_readonly_user` cho query_db — bảng mới cần GRANT (đã đặt
  default privileges; script `create-readonly-role.sh` nên được cập nhật).
- Redis :6380 — broker dramatiq. Queue `default`; XQ (dead-letter) ĐÃ có
  consumer (`run_agent_dead_letter`, on_retry_exhausted).

## Worker & khả năng tự hồi phục

- `run_agent` (workers/agent_runner.py): nhận run_id, spawn CLI trong worktree
  riêng, đọc output, validate RESULT_REF, submit review verdict (nhớ
  `db.flush()` trước — autoflush=False).
- Review artifact lỗi được ghi hai nơi truy vấn được: `tool_metrics.payload`
  (telemetry) và `gate_records.input_payload.error_details` (ledger cùng
  transition). `agent_runs.error_message` chỉ giữ câu tóm tắt. Task trở lại
  `awaiting-review`, còn executor commit range không đổi.
- Outbox: `record_run_requested` cùng transaction với AgentRun; publisher gửi
  message chưa có `dramatiq_message_id`. Send sync ở call-site PHẢI ghi
  message_id ngay (không thì double delivery — CTV2-212).
- Reaper (`reap_dead_running_runs`, chạy trong tick reconcile): run `running`
  có PID chết + tuổi >120s → failed, giải phóng slot concurrency.
- Dead-letter: message cạn retry → run failed "dead-lettered after N retries",
  task thoát treo qua service chính thống.
- Watchdog no-progress: xem `03-gates-and-autonomy.md` (bẫy CLI im lặng).

## Phạm vi đo chi phí LLM

- `LLMUsage` của API ghi token/cost do provider API trả về. Với session
  task-scoped, mỗi bản ghi mang `task_id`; turn xử lý một run cụ thể còn mang
  `agent_run_id`.
- CLI Claude chạy bằng subscription vẫn trả một JSON result object. Đã đọc raw
  output thật của run `7832f2f0-1ecb-4523-aefe-2600acbc0da4` qua MCP:
  `vendor_raw_events` có đúng **1 object** (`seq=0`, 4372 bytes), không phải
  một object cho mỗi turn. Top-level keys là `is_error`, `duration_api_ms`,
  `num_turns`, `stop_reason`, `session_id`, `total_cost_usd`, `usage`,
  `modelUsage`, `permission_denials`, `terminal_reason`, `fast_mode_state`,
  `fast_mode_disabled_reason`, `subtype`, `api_error_status`, `result`,
  `ttft_ms`, `ttft_stream_ms`, `time_to_request_ms`, `type`, `duration_ms`,
  `uuid`. `usage` chứa `input_tokens=224`, `output_tokens=69332`,
  `cache_read_input_tokens=13659112`, `cache_creation_input_tokens=172344`,
  cùng `server_tool_use`, `service_tier`, `cache_creation`, `inference_geo`,
  `iterations` và `speed`. `modelUsage` có hai entries: Haiku với
  `costUSD=0.00354` và Sonnet với `costUSD=6.1724496`; top-level
  `total_cost_usd=6.1759896` bằng tổng hai entry đó.
- `parse_cli_token_usage` lấy token từ top-level `usage`, tức tổng của một lần
  gọi CLI/session. Cost Claude ưu tiên top-level `total_cost_usd`, cùng phạm vi
  với token; chỉ khi trường đó vắng mới cộng toàn bộ `modelUsage.*.costUSD`.
  Trước đây parser lấy entry model đầu tiên, nên run trên bị ghi token của cả
  session nhưng cost Haiku riêng lẻ `0.00354`. `LLMUsage` vẫn là một row cho
  mỗi `AgentRun`, và `_record_run_resource_usage` cộng các row khi có nhiều
  row.
- Contract ghi nhận CLI hiện tại là `input_tokens` luôn bao gồm cache và
  `cached_tokens` luôn là tập con của nó. Claude/Agy được chuẩn hóa từ
  `raw_input_tokens + cache_read`; Codex giữ `input_tokens` tổng và Qwen giữ
  tổng input mà CLI đã báo (cache là một phần của tổng đó). `get_stats` cộng
  `input_tokens` giữa các CLI theo contract này, không cộng thêm
  `cached_tokens`; trường `uncached_input_tokens` là `input_tokens - cached_tokens`
  cho các row đã chuẩn hóa.
- Dưới subscription, `total_cost_usd`/`modelUsage.*.costUSD` là **vendor-reported
  USD usage telemetry** do CLI tính theo model; output không chứng minh đó là
  tiền đã trừ, tiền hoá đơn, hay phần còn lại của gói. Nó cũng không phải tổng
  chi phí tài khoản/tháng và không có dữ liệu về trạng thái/bậc gói. Vì vậy
  `LLMUsage.cost_usd` của `operation=cli` không được gọi là tiền thực và không
  được dùng làm ngưỡng an toàn.
- `max_cost_usd_per_task` chỉ cộng `LLMUsage` không phải CLI (chi phí API có
  thẩm quyền). CLI subscription dùng brake `max_tokens_per_task`, mặc định
  20,000,000 và cấu hình được qua `update_settings`; từ 2026-08-04, tổng token là
  `input_tokens + output_tokens` vì `input_tokens` đã bao gồm cache và
  `cached_tokens` chỉ là tập con để phân tích giá. `RunResourceUsage` và
  `get_stats.cost_usd` cũng loại cost CLI khỏi số USD authoritative, nhưng vẫn
  giữ token CLI để quan sát. Đây là chủ ý fail-safe: cost CLI không tạo cảm
  giác an toàn giả.
- `get_stats.cost_scope` là `recorded_api_usage_only` và `cost_status` phân biệt:
  `measured` (API usage/cost authoritative), `partial` (có API usage và có CLI
  run chưa parse), `unmeasured` (chỉ có CLI token hoặc CLI chưa có cost đáng
  tin), `no_data` (không có dữ liệu). `unmeasured_cli_runs` cho biết số run nằm
  ngoài coverage token.
- Trước khi sửa parser ngày 2026-08-04, `llm_usage.input_tokens` trộn hai quy ước:
  Claude/Agy ghi input chưa cache còn Codex/Qwen ghi tổng input đã gồm cache.
  Các row cũ vì vậy không được so sánh chéo CLI. Không backfill tự động: dù có
  thể suy ra CLI từ một số `AgentRun`, raw vendor output không luôn còn đủ để
  xác nhận semantics, và việc sửa ledger lịch sử immutable có rủi ro làm sai số
  liệu đã dùng cho audit. `get_stats` đánh dấu row cũ vi phạm bằng
  `usage_warnings`; chỉ row được ghi sau thay đổi mới có contract thống nhất.

## Coordinator workspace

- Init: `./scripts/init-coordinator-workdir.sh <dir> [ttl]` — sinh AGENTS.md/
  CLAUDE.md/PROJECT.md + `.mcp.json` (token). Chạy coordinator TỪ ĐÓ, không
  phải trong repo này. Workspace hiện tại của user: `~/^Coject-mangment/`
  (trước là `~/ct-coordinator/`).
- Debug nhanh từ shell: FastMCP client python trỏ `http://localhost:8100/mcp`
  với Bearer token đọc từ `.mcp.json` của workspace.

## Import dữ liệu markdown

```bash
backend/venv/bin/python scripts/migrate_md_to_db.py [--dry-run] [--no-clear]
```
- Clear rồi import: task_dependencies, gate_records, tasks, projects, knowledge_items.
- **agents KHÔNG clear** — upsert giữ `api_key`/`base_url`/`success_rate`
  (API agent không có profile md sẽ mất key vĩnh viễn nếu clear — CASCADE
  agent_accounts).
- Sau import tự re-seed `projects.next_task_seq` (không thì create_task sinh
  id trùng).

## Test

```bash
backend/venv/bin/python -m pytest backend/tests -q   # venv chuẩn duy nhất: backend/venv
```
Trước khi approve verdict một task dispatch, tự chạy suite trong worktree/branch
`ct-run/<run_id>` của executor để xác minh độc lập (reviewer read-only thường
không chạy full suite).

## Sự cố đã gặp & phản xạ

| Triệu chứng | Nguyên nhân quen | Xử lý |
|---|---|---|
| Task kẹt queued mãi | Zombie run chiếm slot / message dead-letter (đã có reaper+DLQ handler) | Kiểm `agent_runs` running/queued; giờ tự hồi phục |
| "restart rồi mà hành vi cũ" | Server cũ giữ port, bản mới chết im | ps theo start time, kill đúng pid |
| Review dài bị cancel "no progress" | CLI im lặng + watchdog | setting 2400s; fix chuẩn CTV2-232 |
| Approve xong không đổi gì | Mapping nuốt args (họ CTV2-233/237) | Soi mapping trong execute_tool |
| pgrep/pkill tự match | Pattern nằm trong command string | Bracket trick `[x]` |
| Import xong create_task đụng id | Quên re-seed counter | Script đã tự làm |

## Backlog sống

`docs/plans/GD4-CLEANUP-PLAN.md` (P1 xong; P2/P3 + CTV2-216..237) và task files
`~/projects/control-tower/projects/agenticmatix/tasks/`. Các bug MỞ đáng nhớ:
CTV2-228 (matcher ghi đè agent chọn), 230 (gate trùng), 231 (cancel bỏ rơi task),
232 (heartbeat), 234 (changes-requested → re-dispatch), 235 (task no-commit),
219/220/223 (retry hygiene, agy, reviewer pool).
