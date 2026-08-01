# 03 — Gates, autonomy & brakes

## Gate flow chuẩn (một vòng đời)

```
create_task → [spec/plan: generate_spec_plan hoặc update_task đổ plan+AC]
→ dispatch_task ──(supervised)── gate task:dispatch pending → approve_gate
→ dispatched → run_agent (execute, worktree, commit lên ct-run/<run_id>)
→ awaiting-review — driver TỰ tạo gate review_order pending (approve chính nó,
  đừng gọi request_review nữa kẻo tạo gate trùng — CTV2-230). Payload gate ghi
  `reviewer`, `selection_reason` từ matcher (capability/success rate và ứng viên
  bị loại bởi four-eyes/disabled); `approval_prompt` và `pending_approvals.prompt`
  đều nêu reviewer + lý do để human thấy trước khi approve.
→ in-review → run_agent (review, read-only, viết JSON vào .ct/review-<task>.json)
→ reviewer submit verdict → gate task:verdict pending → approve_gate
→ verdict pass → done   |   verdict fail → changes-requested
```

## Spec Clarity Loop (CTV2-242)

`generate_spec_plan` là research-first gate: chỉ nhận agent CLI và spawn agent
với `cwd=Project.repo_root`. Prompt bắt agent đọc read-only README/docs/entry
points rồi lần source liên quan trước khi lập plan. API-backed agent bị từ chối
vì không thể đọc repository.

Output strict schema v1.1 bắt buộc có `spec_clarity` (`high|medium|low`) và
`open_questions` (list, rỗng khi không còn câu hỏi). Nếu còn câu hỏi hoặc clarity
khác `high`, task giữ `todo` nhưng bật escalation `awaiting_approval`; tool trả
`action=spec_questions_pending` cùng toàn bộ câu hỏi để coordinator hỏi human
ngay. Coordinator ghi câu trả lời bằng `update_task.patch.raw_input` (replace),
rồi chạy lại `generate_spec_plan`. Chỉ kết quả `high` + danh sách rỗng mới clear
escalation và trả `spec_plan_generated`.

`dispatch_task` execute kiểm tra độc lập cột `open_questions` và từ chối trước
dispatch nếu còn câu hỏi. Mỗi lần generate ghi metric `spec_plan` gồm clarity và
số câu hỏi.

**Landing (CTV2-238): done = code ĐÃ ở main.** Khi verdict gate approve với
pass, HỆ THỐNG (git subprocess thuần trong `services/landing.py` — không LLM,
không coordinator) merge `--no-ff` head của result_ref vào branch đang
checkout ở repo_root, ghi merge commit vào `task.landed_ref`, xóa các branch
`ct-run/*` đã merge:
- Merge sạch (hoặc head đã là ancestor — idempotent) → `done` + `landed_ref`
  + event `landed`.
- Conflict / cây tracked bẩn / detached HEAD → task KHÔNG done: escalation
  `awaiting_approval` với lỗi git + event `landing_failed`; sửa repo xong gọi
  tool `land_task {task_id}` để thử lại (tool này cũng backfill được task done
  cũ chưa merge).
- repo_root không phải git repo / result_ref không có head (legacy import) →
  landing skip, done như cũ (giữ fixture test và data import sống).
Coordinator vẫn bị cấm tự chạy `git merge` — rule giữ nguyên.

Bẫy liên quan (đã dính khi build landing): `emit_task_event` COMMIT nội bộ,
và `trg_tasks_done_verdict` là DEFERRED trigger (chạy lúc commit) đòi record
verdict approved tồn tại — vì vậy KHÔNG được emit event giữa chừng
`_apply_gate` trước khi `_ledger_record` ghi row quyết định; event landing
được stash và phát trong `decide_gate` sau ledger record.

## Mode

- **supervised**: mỗi gate cần human `approve_gate`.
- **bypass**: gate tự approve (dùng cho auto trong ngân sách rủi ro).
- **plan-only**: chặn dispatch.
- Resolve: setting `autonomy` (plan-only|supervised|auto) + so `task.risk` với
  `auto_max_risk`. Auto + risk vượt ngưỡng → hạ về supervised.

## approve_gate — ngữ nghĩa quan trọng

- Args: `gate_record_id` (hoặc `task_id` → gate pending của task; dạng
  `admin:<id>` cho admin gate) + **`decision`: approved | rejected**.
- LỊCH SỬ ĐEN (CTV2-233): schema từng KHÔNG có `decision` — mọi reject bị ghi
  thành approve. Đã sửa; nếu thấy hành vi lạ quanh reject, nghi chỗ này trước.
- Approve dispatch/review_order = REPLAY payload đã ghi trong gate → tạo AgentRun.
  `agent_id`/`executor`/`reviewer` chỉ định được tôn trọng (CTV2-228 đã sửa —
  mapping nhận cả alias `agent_id`); matcher chỉ chạy khi không chỉ định.
  Reviewer chỉ định không tồn tại, disabled, hoặc trùng executor bị từ chối rõ
  ràng kèm tối đa 3 reviewer hợp lệ, tuyệt đối không tự thay bằng agent khác.
  Kết quả approve `review_order` trả lại `reviewer` + `selection_reason` từ
  payload bất biến của gate.
- Verdict fail → changes-requested → `dispatch_task` chấp nhận re-dispatch
  thẳng từ đó (vòng replan, CTV2-234 đã sửa — constraint terminal chỉ còn
  áp cho done, migration 037).
- Task read-only: tag `no-commit` + executor in `RESULT_REF: none` (worktree
  phải thật sự không có commit) → done qua gate verdict hệ thống
  (reviewer `@system-no-commit`, minh bạch trong ledger — CTV2-235).

## Admin gates

`manage_agent` / `manage_project` / `manage_knowledge` / `update_settings` đều
tạo AdminGateRecord pending → `approve_gate {"task_id": "admin:<id>"}`.
Update nhận cả dạng phẳng lẫn `{id, patch}` (nested từng bị nuốt lặng lẽ —
CTV2-237, đã sửa; update rỗng giờ báo lỗi).

## Escalation

Brake đạp (round limit, cost, review result hỏng...) → task set
`awaiting_approval=true` + `approval_prompt`, KHÔNG tạo GateRecord (CTV2-221).
Spec Clarity Loop dùng cùng escalation này khi spec chưa đạt `high` hoặc còn
`open_questions`; approval prompt liệt kê đủ câu hỏi và yêu cầu generate lại sau
khi cập nhật `raw_input`.
Vì vậy `_pending_approvals` (mcp_native) quét thêm nhánh escalation
(`kind: "task:escalation"`). `auto_max_rounds` (default 3) round
changes-requested → status `failed` + escalation "human replan".

## Brakes (`check_brakes`, task_orchestration ~1580)

Thứ tự kiểm tra: dependencies pending → `autonomy_enabled=false` STOP →
cost ≥ `max_cost_usd_per_task` STOP → agent tồn tại/available → account health →
per-run: `max_active_seconds_per_run`, `max_tool_calls_per_run`,
`max_no_progress_seconds` → `max_concurrent_runs` (QUEUE).

**Bẫy no-progress (CTV2-232)**: "progress" = `run.updated_at` nhích, mà
`claude -p` KHÔNG in gì cho đến khi xong → run dài im lặng bị cancel oan
("Run made no progress within the allowed interval"). Tạm thời setting
`max_no_progress_seconds=2400`. Fix chuẩn (chưa làm): heartbeat theo PID sống.
Hệ quả kép CTV2-231 (chưa sửa): run review bị cancel để task KẸT in-review —
phải SQL về awaiting-review rồi request_review lại.

## Settings (SETTINGS_WHITELIST, entity_admin.py)

`autonomy`, `auto_max_risk`, `auto_max_rounds`, `autonomy_enabled`,
`max_cost_usd_per_task`, `max_concurrent_runs` (default 2),
`run_timeout_seconds` (900), `max_active_seconds_per_run`,
`max_tool_calls_per_run`, `max_no_progress_seconds` (default 300; đang set 2400),
`sql_timeout_seconds`, `sql_row_cap`, `context_snapshot_top_n`,
`default_coordinator_model`, `default_mode` (CHẾT — đừng dùng).
Ghi qua `update_settings` → admin gate.

## Nhắc nợ human (server-side)

Mọi tool result đính `pending_approvals` (+ note tiếng Việt yêu cầu coordinator
nhắc lại ở CUỐI mỗi câu trả lời, dạng câu hỏi) — gồm task gates mở, admin gates
mở, và escalations; đã lọc task archived. Xem `_pending_approvals` trong
`mcp_native.py`.
