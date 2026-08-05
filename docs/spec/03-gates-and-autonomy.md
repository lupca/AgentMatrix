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

Review artifact dùng schema strict (`extra="forbid"`). Ba alias metadata đã đo
từ Claude Opus được chuẩn hóa có kiểm soát vào `toolchain_results`:
`toolchain_output` (object), `toolchain_notes` (string), và `notes` (string).
Field lạ khác vẫn bị từ chối; prompt đồng thời cấm thêm top-level key ngoài
contract. Nếu parse/schema/AC-count vẫn lỗi, review run thành `failed`, chi tiết
Pydantic được ghi vào JSON payload của `review_result/rejected` và
`tool_metrics.payload`; Task CAS về `awaiting-review` thay vì `failed`, giữ
nguyên commit range để coordinator giao reviewer khác ngay.

## Spec Clarity Loop (CTV2-242)

`generate_spec_plan` là research-first gate: chỉ nhận agent CLI và spawn agent
với `cwd=Project.repo_root`. Prompt bắt agent đọc read-only README/docs/entry
points rồi lần source liên quan trước khi lập plan. Planner đồng thời phải gọi
MCP native `spec_get(filter.project_id=task.project)` trực tiếp trước khi kết
luận `prior_art`/`constraints`, theo thứ tự: `conflicts_with` +
constraint, requirement/design, anchor file/symbol, rồi task link. Critic cũng
phải tra kho spec trước khi đánh giá `prior_art`; câu trả lời chỉ dựa vào code
không thay thế được bước này. API-backed agent bị từ chối vì không thể đọc
repository.

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

### Plan critic: hai bước nối qua DB, không qua biến trong bộ nhớ (CTV2-1378)

Bên trong `generate_spec_plan` KHÔNG còn hai `await` dính liền trong một giao
dịch DB. Từ bên ngoài tool contract không đổi (vẫn planner → critic → trả về
một lần), nhưng nội bộ đi qua DB làm điểm nối:

1. **Bước plan**: `spec_plan_generator.generate_spec_plan()` chạy LLM, rồi
   `TaskStateMachine.write_spec_plan()` ghi `acceptance_criteria/constraints/
   evidence/plan/files/tests/risk/flows/spec_clarity/open_questions/planner`
   vào task, append GateRecord `spec_plan`, và **commit ngay** — plan đã bền
   vững trên DB, `plan_critic_status` còn NULL.
2. **Bước critic**: `spec_plan_generator.spec_plan_result_from_task(task)`
   đọc lại plan vừa ghi (từ cột DB, không phải object trong bộ nhớ) để dựng
   lại `SpecPlanResult`, rồi `criticize_spec_plan()` chạy critic độc lập, và
   `TaskStateMachine.record_plan_critic_verdict()` ghi `plan_critic/
   plan_critic_status/plan_critic_findings` + append đúng một GateRecord
   `plan_critic` mỗi lần gọi (four-eyes qua `require_independent(task.planner,
   critic)` đọc từ DB, không tin tham số gọi vào).

Nếu bước critic lỗi (`PlanCriticError`/`ConfigurationError`), `generate_spec_plan`
trả `{'error': ..., 'plan_persisted': True, 'next': 'critique_spec_plan'}` —
**plan vẫn nằm nguyên trên task**, không mất, không phải chạy lại planner.

**Tool mới: `critique_spec_plan {task_id, critic_id?}`** — chạy lại riêng bước
critic trên plan đang có sẵn trên task, không bao giờ gọi planner. Dùng để
retry sau khi critic lỗi, hoặc chạy thêm vòng critique sau một verdict reject.
Mỗi lần gọi (kể cả gọi lại nhiều lần) append thêm một GateRecord `plan_critic`
mới — vòng critique luôn đếm được qua
`SELECT count(*) FROM gate_records WHERE gate_type='plan_critic' AND task_id=...`,
không có trần cứng số vòng (chỉ brake token/cost hiện có mới chặn).

Không có transaction dài xuyên suốt hai lệnh gọi LLM nữa — mỗi bước commit
riêng, nên một critic bị treo/timeout không còn giữ khoá DB xuyên suốt lượt
planner + critic như trước.

### Planner/critic đi qua outbox + worker, không còn chạy trong MCP server (CTV2-1382)

CTV2-1378 đưa plan/critic thành hai bước nối qua DB nhưng cả hai vẫn `await`
đồng bộ **trong tiến trình MCP server**: mỗi `AgentRun` do
`spec_plan_generator._begin_llm_run` tạo (`idempotency_key LIKE 'planner:%'`)
không có `pid`/`started_at` — CLI con là con trực tiếp của PID MCP server, và
client phải đợi 170-420s một lần gọi. CTV2-1382 chuyển việc CHẠY (không phải
việc build prompt) sang đúng cơ chế dùng cho mọi run khác:

1. `_handle_generate_spec_plan` build placeholder `AgentRun` (kind=`execute`,
   `idempotency_key = planner:<task_id>:plan:<uuid>`, status=`queued`), ghi
   `OutboxEvent(event_type='run_requested')` qua `record_run_requested` +
   commit, gọi `run_agent.send()` (fast path), rồi CHỜ tối đa **30 giây**
   (poll DB, không giữ transaction).
2. Dramatiq actor `run_agent` (`app.workers.cli_executor.execute_agent_run`)
   nhận diện run này qua `plan_executor.is_plan_run` (prefix
   `idempotency_key`) và rẽ sang `plan_executor.execute_plan_run` thay vì
   luồng worktree/git-diff — planner không tạo worktree, không có diff.
   `generate_spec_plan()`/`criticize_spec_plan()` chạy KHÔNG ĐỔI logic bên
   trong (`asyncio.run()` trong worker), chỉ khác nơi chạy; `on_start` được
   nối xuyên `LLMService → CLIProvider → CLIDispatcher.spawn → ProcessManager`
   để ghi `pid` thật của tiến trình CLI ngay khi spawn.
3. Plan xong (`write_spec_plan` commit) → worker TỰ dispatch bước critic
   (`plan_executor.create_critic_run`, cùng cơ chế outbox) — client không
   phải gọi lại. Chọn critic: nếu `critic_id` được truyền tường minh, giữ
   nguyên qua một `TaskEvent(event_type='spec_plan_dispatch_context')`; nếu
   không, worker tự chọn lại (`AgentSuggester role=reviewer`) tại thời điểm
   dispatch critic — mới hơn, không lệ thuộc snapshot lúc gọi ban đầu.
4. Nếu cả hai bước xong trong 30s: `generate_spec_plan` trả nguyên payload
   `SpecPlanResult` như trước (không đổi hợp đồng cho client cũ). Nếu không,
   trả handle `{run_id, task_id, status, next: 'wait_for_task', latest_run}`
   — gọi tiếp `wait_for_task` để lấy kết quả khi worker xong.

Vì `Task.status` không đổi trong suốt lúc lập plan (giữ `todo`), một
`AgentRun` planner/critic KHÔNG BAO GIỜ được xử lý lỗi qua
`TaskOrchestrationService.record_execution_failure`/`record_review_failure`/
`record_dispatch_queue_failure` (các hàm này CAS `Task.status` từ
`dispatched`/`in-review`, luôn ném lỗi với task `todo`). Runner chết giữa
chừng (kill CLI, restart backend, outbox dead-letter) chỉ đánh dấu MỖI
`AgentRun` là `failed` cục bộ — `outbox.py:_reap_run`, `outbox.py:_dead_letter`,
`agent_runner.py:run_agent_dead_letter`, và `cli_executor.py:
_record_unexpected_failure` đều rẽ nhánh qua `plan_executor.is_plan_run`
trước khi gọi các hàm CAS task ở trên.

**Landing (CTV2-238): done = code ĐÃ ở main.** Khi verdict gate approve với
pass, HỆ THỐNG (git subprocess thuần trong `services/landing.py` — không LLM,
không coordinator) merge `--no-ff` head của result_ref vào branch đang
checkout ở repo_root, ghi merge commit vào `task.landed_ref`, xóa các branch
`ct-run/*` đã merge:
- Merge sạch (hoặc head đã là ancestor — idempotent) → `done` + `landed_ref`
  + event `landed`; diff file của range được đối chiếu `spec_anchor.path` và
  tự ghi cạnh `spec_task_link(modifies, confidence=derived)` cho các spec cùng
  project. Retry/backfill không tạo cạnh trùng.
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
  áp cho done, migration 037). Re-dispatch execute lấy head của round vừa
  review (ưu tiên `TaskRound.result_ref`, fallback `Task.result_ref`) làm base
  của worktree `ct-run/<run_id>`, nên executor tiếp tục trên commit cũ thay vì
  bắt đầu lại từ main. Prompt cũng có mục **Review feedback to address** với
  verdict và từng finding theo file/line/severity/description; full history
  của các round cũ không thuộc scope.
- Reject một verdict gate không được giữ Task ở `in-review`: FSM CAS về
  `awaiting-review`, đặt `current_gate=review_order`, clear verdict projection,
  và MCP trả `next` yêu cầu gọi `request_review`. Review run cũ vẫn là bằng
  chứng immutable; review attempt mới dùng số attempt tiếp theo và vẫn phải
  tuân four-eyes.
- Task read-only: tag `no-commit` + executor in `RESULT_REF: none` (worktree
  phải thật sự không có commit) → done qua gate verdict hệ thống
  (reviewer `@system-no-commit`, minh bạch trong ledger — CTV2-235).

## Admin gates

`manage_agent` / `manage_project` / `manage_knowledge` / `update_settings` đều
tạo AdminGateRecord pending → `approve_gate {"task_id": "admin:<id>"}`.
Update nhận cả dạng phẳng lẫn `{id, patch}` (nested từng bị nuốt lặng lẽ —
CTV2-237, đã sửa; update rỗng giờ báo lỗi).

## Escalation

Brake đạp (round limit, cost...) → task set
`awaiting_approval=true` + `approval_prompt`, KHÔNG tạo GateRecord (CTV2-221).
Spec Clarity Loop dùng cùng escalation này khi spec chưa đạt `high` hoặc còn
`open_questions`; approval prompt liệt kê đủ câu hỏi và yêu cầu generate lại sau
khi cập nhật `raw_input`.
Vì vậy `_pending_approvals` (mcp_native) quét thêm nhánh escalation
(`kind: "task:escalation"`). `auto_max_rounds` (default 3) round
changes-requested → status `failed` + escalation "human replan".

## Brakes (`check_brakes`, task_orchestration ~1580)

Thứ tự kiểm tra: dependencies pending → `autonomy_enabled=false` STOP →
authoritative API cost ≥ `max_cost_usd_per_task` STOP → token total ≥
`max_tokens_per_task` STOP → agent tồn tại/available → account health →
per-run: `max_active_seconds_per_run`, `max_tool_calls_per_run`,
`max_no_progress_seconds` → `max_concurrent_runs` (QUEUE).

**Bẫy no-progress (CTV2-232/CTV2-231)**: CTV2-232 bơm `run.updated_at` khi PID
còn sống và dùng stream-json để tránh false positive. Nếu watchdog vẫn chặn một
run trước lúc spawn/re-spawn, worker không chỉ đổi projection AgentRun: review
cancel đi qua `record_review_failure` về `awaiting-review`; execute cancel đi
qua `record_execution_failure` về `failed`. Cả hai ghi nguyên nhân brake vào
Task + ledger, nên không còn Task `in-review`/`dispatched` không có đường tiếp.

## Settings (SETTINGS_WHITELIST, entity_admin.py)

`autonomy` (nút thật cho mode: `supervised`/`auto`/`plan-only`),
`auto_max_risk`, `auto_max_rounds`, `autonomy_enabled`,
`max_cost_usd_per_task`, `max_tokens_per_task` (default 20,000,000),
`max_concurrent_runs` (default 2),
`run_timeout_seconds` (900), `max_active_seconds_per_run`,
`max_tool_calls_per_run`, `max_no_progress_seconds` (default 300; đang set 2400),
`sql_timeout_seconds`, `sql_row_cap`, `context_snapshot_top_n`,
`default_coordinator_model`.
Ghi qua `update_settings` → admin gate.

`default_mode` đã bị gỡ khỏi whitelist (CTV2-222): không ghi được nữa; các
row Setting cũ còn trong DB chỉ để đọc lịch sử, không phải nguồn policy.
Mode thật do `autonomy` (+ `auto_max_risk` so với `task.risk`) quyết định.

## Nhắc nợ human (server-side)

Mọi tool result đính `pending_approvals` (+ note tiếng Việt yêu cầu coordinator
nhắc lại ở CUỐI mỗi câu trả lời, dạng câu hỏi) — gồm task gates mở, admin gates
mở, và escalations; đã lọc task archived. Xem `_pending_approvals` trong
`mcp_native.py`.
