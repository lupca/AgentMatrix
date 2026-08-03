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

- Postgres: container **`control_tower_db`**, port 5433, user `ct`.
  `docker exec -i control_tower_db psql -U ct -d control_tower`.
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

- `llm_usage` chỉ ghi token/cost khi provider API trả usage có thẩm quyền. Với
  session task-scoped, mỗi bản ghi mang `task_id`; turn xử lý một run cụ thể còn
  mang `agent_run_id`. Brake `max_cost_usd_per_task` vì vậy chỉ phản ánh phần
  chi phí API đã ghi nhận.
- Agent CLI executor (`claude`, `agy`, `codex`, `qwen`) hiện chạy bằng
  subscription và command stdout hiện tại không cung cấp usage/cost có thẩm
  quyền. Không parse ước lượng từ độ dài text và không sinh `LLMUsage` giả cho
  các run này. `RunResourceUsage.estimated_cost_usd = 0` trong trường hợp đó có
  nghĩa là **không đo được**, không có nghĩa run miễn phí.
- `get_stats.cost_scope` là `recorded_api_usage_only` và `cost_status` phân biệt:
  `measured` (có usage API, kể cả cost thật bằng 0), `partial` (có usage API và
  có CLI run chưa đo), `unmeasured` (chỉ có CLI run), `no_data` (không có dữ
  liệu). `unmeasured_cli_runs` cho biết số run nằm ngoài coverage của cost.

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
