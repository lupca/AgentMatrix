# 03 — Gates, autonomy & brakes

## Gate flow chuẩn (một vòng đời)

```
create_task → [spec/plan: generate_spec_plan hoặc update_task đổ plan+AC]
→ dispatch_task ──(supervised)── gate task:dispatch pending → approve_gate
→ dispatched → run_agent (execute, worktree, commit lên ct-run/<run_id>)
→ awaiting-review — driver TỰ tạo gate review_order pending (approve chính nó,
  đừng gọi request_review nữa kẻo tạo gate trùng — CTV2-230)
→ in-review → run_agent (review, read-only, viết JSON vào .ct/review-<task>.json)
→ reviewer submit verdict → gate task:verdict pending → approve_gate
→ verdict pass → done   |   verdict fail → changes-requested
```

**⚠️ "done" hiện KHÔNG có nghĩa code đã vào main.** Commit của executor nằm
trên `ct-run/<run_id>`; coordinator bị cấm merge (rule hậu agy-incident) và
chưa có actor chính thống nào merge thay → admin phải merge tay
(`git merge --no-ff ct-run/<run_id>`) sau khi done. Đây là gap thiết kế
CTV2-238 (đề xuất: bước landing do worker thực hiện sau verdict pass, có gate
merge trong supervised, conflict thì escalate).

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
  Đã biết (CTV2-228, chưa sửa): `agent_id` coordinator yêu cầu lúc dispatch
  KHÔNG được tôn trọng khi replay — matcher tự chọn lại.
- Reject verdict → task về changes-requested; KHÔNG có đường chính thống
  `changes-requested → dispatch` (dispatch_task đòi todo — CTV2-234, tạm phải
  SQL về todo).

## Admin gates

`manage_agent` / `manage_project` / `manage_knowledge` / `update_settings` đều
tạo AdminGateRecord pending → `approve_gate {"task_id": "admin:<id>"}`.
Update nhận cả dạng phẳng lẫn `{id, patch}` (nested từng bị nuốt lặng lẽ —
CTV2-237, đã sửa; update rỗng giờ báo lỗi).

## Escalation

Brake đạp (round limit, cost, review result hỏng...) → task set
`awaiting_approval=true` + `approval_prompt`, KHÔNG tạo GateRecord (CTV2-221).
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
