# CTV2-1369 — Rà soát CTV2-218..231 trước khi giao

Ngày rà soát: 2026-08-04. Code được đọc tại `d72fb34` (`main`). Đây chỉ là
báo cáo; không có bug fix, migration, thay đổi dữ liệu hay `archive_task`.

## Kết luận

| Task | Phân loại | Mức độ nếu còn sống | Phạm vi sửa |
|---|---|---:|---:|
| CTV2-218 | **CÒN SỐNG** | medium | nhỏ |
| CTV2-219 | **CÒN SỐNG** | high | lớn |
| CTV2-220 | **CÒN SỐNG** | high | vừa |
| CTV2-221 | **ĐÃ XONG** | — | — |
| CTV2-222 | **CÒN SỐNG** | medium | nhỏ |
| CTV2-223 | **CÒN SỐNG** | medium | vừa |
| CTV2-224 | **CÒN SỐNG** | medium | vừa |
| CTV2-225 | **CÒN SỐNG** | critical | vừa để điều tra; có thể lớn sau khi biết đường vào |
| CTV2-226 | **CÒN SỐNG** (một phần mô tả retry đã hết) | medium | nhỏ |
| CTV2-230 | **ĐÃ XONG** | — | — |
| CTV2-231 | **CÒN SỐNG** | high | vừa |

Không task nào thuộc **HẾT LIÊN QUAN**. Kiến trúc MCP-native làm CTV2-226 trở
thành code legacy cần bỏ, chứ không làm vấn đề biến mất: actor đó vẫn được gọi
và vẫn khởi động coordinator nội bộ.

## Phương pháp và giới hạn bằng chứng

- Đọc mô tả gốc trong snapshot task ở
  `backups/control_tower_20260802_162652.sql`, rồi đọc implementation hiện tại;
  không kết luận chỉ từ `git log` hoặc tài liệu kế hoạch.
- Dùng test hiện có để kiểm chứng các transition đã sửa. Không thêm test tái
  hiện đỏ vì task này không sửa bug.
- Không truy vấn DB trực tiếp: đó là hard boundary của dự án. Những dữ liệu live
  sau snapshot (inbox `f53df48a`, năm task có commit trên main nhưng không verdict,
  và CTV2-010/069 bị kẹt) là bằng chứng vận hành do đề bài cung cấp; báo cáo phân
  biệt rõ chúng với điều suy ra từ code.

## Rà từng task

### CTV2-218 — CÒN SỐNG

Mô tả gốc: mỗi lần worker boot/re-deliver tạo thêm một chuỗi polling
`outbox_publisher` và `reconcile_orphaned_agent_runs`, từng quan sát 166+166
chuỗi và log storm.

Bằng chứng code hiện tại:

- `_OutboxPollerBootstrap.after_worker_boot` vẫn gửi cả hai actor vô điều kiện ở
  `backend/app/workers/__init__.py:31-38`.
- Cả hai actor vẫn tự gửi lại chính mình trong `finally` ở
  `backend/app/workers/outbox_publisher.py:35-48` và `:51-77`. Không có Redis
  singleton lock, fixed message id hay lease generation.
- Actor vẫn trả `dict` (`:36`, `:42`, `:52`, `:71`), nên phần cảnh báo
  “returned a value that is not None” cũng chưa được xử lý nếu Results middleware
  không được cài.

Cách tái hiện: boot hai worker (hoặc gọi bootstrap hai lần), theo dõi queue sau
mỗi poll interval; mỗi seed tự sinh hậu duệ riêng, không có điểm hội tụ/dedupe.
Không cần side effect nghiệp vụ để tái hiện vì actor tự reschedule cả khi DB rỗng.

Phạm vi: **nhỏ** — một cơ chế singleton lease/message-id dùng chung cho mỗi poller,
test boot/redelivery, và trả `None` (hoặc cài Results middleware).

### CTV2-219 — CÒN SỐNG

Mô tả gốc gồm sáu mảnh liên quan retry: lock theo run, idempotency theo attempt,
branch/worktree, cấm fallback shared tree, run outlive task, và cấp sequence an
toàn khi đua.

Code hiện tại mới sửa một phần:

- Có terminal-delivery guard ở `backend/app/workers/cli_executor.py:759-769` và
  branch cũ bị xóa trước khi recreate ở
  `backend/app/services/process_manager.py:336-370`.
- Nhưng lần đọc `AgentRun` ở `cli_executor.py:760` không `FOR UPDATE`, advisory
  lock hay compare-and-set. Hai delivery cùng thấy `queued` vẫn có thể cùng đi
  qua và cùng ghi `running` ở `:795-802`, `:879-882`.
- `WorktreeUnsupportedError` vẫn fallback thẳng về repo dùng chung ở
  `cli_executor.py:847-862`, trái hard boundary worktree-per-run.
- Idempotency review của driver vẫn chỉ theo task/round
  (`backend/app/workers/agent_runner.py:490-497`), không chứa review attempt hay
  reviewer. Đổi reviewer giữa vòng có thể tái dùng key với input khác.

Bằng chứng vận hành: inbox `f53df48a` ghi nhận ngày 2026-08-04 việc đổi reviewer
đụng key `advance:CTV2-232:review:r2`, làm CTV2-232 chết sau khi review đã chạy
xong và tiêu thụ 291.883 token. Đây là tái hiện live của nhánh idempotency theo
round, không phải dữ liệu từ backend cũ.

Phạm vi: **lớn** — tách thành các AC nhưng giao chung vì cùng invariant retry:
claim/lease atomically theo run+attempt, key theo attempt/input ổn định, fail-hard
khi không tạo được worktree, lifecycle cleanup/cancel, và test duplicate delivery
thật với hai DB session.

### CTV2-220 — CÒN SỐNG

Mô tả gốc: `agy --print` có thể bỏ cwd được spawn để làm trong scratch workspace;
đồng thời mapping model/effort và eligibility chưa an toàn.

Bằng chứng:

- Cả dispatch lẫn review vẫn dựng `agy ... --print` rồi chỉ trả `repo_root` làm
  cwd (`backend/app/services/command_builder.py:111-132`, `:203-222`). Không có
  workspace registration/flag/handshake để chứng minh agy đang thao tác đúng
  worktree.
- Matcher không loại agy khỏi executor/reviewer. Eligibility hiện chỉ xét status,
  four-eyes và capability tùy chọn
  (`backend/app/services/agent_matcher.py:211-228`); driver review còn gọi matcher
  mà không yêu cầu capability riêng.
- Catalog model agy hiện đã tốt hơn, nhưng không giải quyết scratch cwd.

Tái hiện/dữ liệu thật: CTDE-009 run `42c41a66` đã tạo commit trong
`~/.gemini/antigravity-cli/scratch/` thay vì worktree; CTV2-227 vòng 2 còn cho
thấy agy rubber-stamp review. Muốn tái hiện lại: dispatch một agy executor tạo
file sentinel chỉ có trong worktree, rồi so sánh `git -C <worktree> HEAD` với ref
agy trả. Không chạy lại ở audit này vì sẽ dispatch agent thật và tốn tiền.

Phạm vi: **vừa** — trước hết fail-closed eligibility cho agy execute/review; sau
đó nghiên cứu workspace contract và test canary cwd trước khi mở lại.

### CTV2-221 — ĐÃ XONG

Mô tả gốc: escalation đặt `awaiting_approval=true` nhưng không tạo pending
`GateRecord`, khiến `approve_gate` không có gì để quyết định và cờ ma chặn dispatch.

`66290e8` chỉ làm escalation không gate hiện lên `pending_approvals`; nó tăng
visibility nhưng chưa tự làm escalation approvable. Fix đóng lỗi thực tế nằm ở
FSM hiện tại:

- `ffc17dfd218b816410463b16e77cd80ca46e42d3` đưa escalation qua
  `TaskStateMachine.escalate_task`: tạo ledger row `escalation/rejected`, rồi gọi
  `_sync_after_transition` (`backend/app/services/task_state_machine.py:1487-1512`).
- `sync_awaiting_approval` tính cờ chỉ từ pending root chưa có child
  (`task_state_machine.py:270-282`); terminal transition reject mọi pending gate
  append-only rồi clear projection (`:321-337`). Vì escalation row là rejected,
  task kết thúc `failed`, `awaiting_approval=false`, không còn cờ ma và không có
  lời mời approve giả.
- Regression test `backend/tests/test_task_orchestration.py:115-135` kiểm đúng
  `failed`, `awaiting_approval is False`, prompt clear và ledger row rejected.

Kết luận chọn phương án (b) của task gốc: không giữ `awaiting_approval` khi không
có gate thật. Commit visibility `66290e8` còn là fallback cho legacy rows nhưng
không phải lý do chính để đóng task.

### CTV2-222 — CÒN SỐNG

Mô tả gốc: `default_mode` được quảng cáo như setting điều khiển gate nhưng code
thật đọc `autonomy` và risk.

Bằng chứng code/tái hiện:

- `default_mode` vẫn nằm trong whitelist với mô tả sai ở
  `backend/app/services/entity_admin.py:427-430`.
- `resolve_autonomy` chỉ đọc `autonomy`, `auto_max_risk`, `auto_max_rounds` ở
  `backend/app/services/task_validators.py:126-159`; `mode_for_task` ở `:161-174`
  không đọc `default_mode`.
- Test matrix hiện có tự tái hiện: với `autonomy=auto`, task low/normal trả
  `bypass` (`backend/tests/test_task_orchestration.py:1449-1475`) bất kể
  `default_mode` được ghi gì.

Phạm vi: **nhỏ** — bỏ `default_mode` khỏi whitelist/query docs và migration-free
backward compatibility warning; mô tả `autonomy` là nút thật. Không nên nối thêm
một nguồn policy thứ hai.

### CTV2-223 — CÒN SỐNG

Mô tả gốc: reviewer auto-selection có thể chọn id người/seed rác như `@user`,
`@lupca`, `@gpt-5.6-sol` vì pool không chứng minh đó là reviewer CLI thật.

Bằng chứng code:

- Matcher query toàn bộ bảng agents (`backend/app/services/agent_matcher.py:138`)
  và eligibility chỉ loại status unavailable, executor trùng, hoặc thiếu
  capability khi caller có truyền requirement (`:211-228`). Không kiểm
  `agent_type=cli`, `cli` được hỗ trợ, role reviewer hay capability review.
- Driver `_advance_awaiting_review` gọi `score_candidates(...,
  exclude_agent_id=task.executor)` mà không có `required_capabilities`, rồi lấy
  hạng đầu (`backend/app/workers/agent_runner.py:459-473`).

Cách tái hiện: seed một `Agent(id="@user", status="idle")` không CLI/role review,
cho nó score tốt hơn các agent khác rồi gọi review auto-selection; nó vẫn eligible.
Dữ liệu CTDE-003/001 trong mô tả gốc đã quan sát `@user` và `@gpt-5.6-sol` được
gán reviewer.

Phạm vi: **vừa** — định nghĩa eligibility executable/reviewable một chỗ, validate
CLI/model/capability, dùng cùng predicate cho matcher, explicit reviewer và
suggestion; archive seed rác là cleanup dữ liệu riêng.

### CTV2-224 — CÒN SỐNG

Mô tả gốc: không có tool chính chủ để đổi mode một task hiện hữu; thay đổi phải
qua gate/audit chứ không cho update tự do.

Bằng chứng/tái hiện trực tiếp:

- Tool schema chỉ mô tả raw input, plan, AC, priority, tags, dependencies
  (`backend/app/services/tool_registry.py:782-805`).
- FSM whitelist patch chỉ có `plan`, `acceptance_criteria`, `priority`, `tags`,
  `raw_input`; patch `mode` trả `Cannot patch fields: mode`
  (`backend/app/services/task_state_machine.py:1614-1647`).
- Không có gate type/handler nào cho mode change. Gọi
  `update_task({"patch":{"mode":"bypass"}})` là reproducer tối thiểu.

Phạm vi: **vừa** — chốt semantics mode trước, thêm ToolSpec + handler qua FSM,
validate ba mode, CAS/version, pending gate append-only, approve/reject replay và
test transition/four-eyes phù hợp. Không được chỉ thêm `mode` vào patch whitelist.

### CTV2-225 — CÒN SỐNG

Mô tả gốc hẹp là bypass run thiếu `result_ref`. Nhánh executor hiện tại đã tốt hơn:
worker dựng `base..head` và gọi `record_execution_success`
(`backend/app/workers/cli_executor.py:1138-1172`), còn `attach_result` sau
`b7a0f3c`/merge `0ff0f62` chỉ cho `dispatched -> awaiting-review` và từ chối
`option=done` (`backend/app/services/task_state_machine.py:1973-2042`; regression
tests `backend/tests/test_attach_result.py:61-111`). `land_task` cũng gọi
`require_approved_pass_verdict` trước merge (`task_state_machine.py:1862-1885`).

Tuy vậy không thể đóng task điều tra vì có bằng chứng live ngày 2026-08-04:
CTV2-010 (`01d4a08`), CTV2-069 (`0f62f73`), CTV2-093 (`173b85f`), CTV2-232
(`8a9c4a2`) và CTV2-1359 (`3c0c3e3`) đều có commit reachable từ main trong khi
`landed_ref=null` và không có pass verdict. Ba ca đầu là legacy (26–27/07), nhưng
CTV2-232/1359 phát sinh cùng ngày và không thể giải thích chỉ bằng legacy import.
Audit git xác nhận cả năm SHA là ancestor của HEAD `d72fb34`.

Do đó bug lõi còn sống dưới dạng **drift main-vs-ledger/four-eyes bypass**, dù
chưa chứng minh nó còn đi qua nhánh `run.result_ref` gốc. Reproducer điều tra:
lấy task không có pass verdict, ghi nhận HEAD, chạy từng đường hợp lệ
`record_execution_success -> request_review -> verdict -> land_task`, rồi đối
chiếu task/gate/outbox event với `git merge-base --is-ancestor`; bất kỳ commit
mới nào lên integration branch trước pass verdict là đường thủng. Đồng thời truy
ngược audit/outbox của CTV2-232 và CTV2-1359 trước, vì chúng mới và phân biệt được
manual merge, landing cũ hay worker side effect.

Phạm vi: **vừa để điều tra**, có thể **lớn** nếu cần reconciliation/enforcement
giữa git main và ledger. Mức độ **critical** vì vi phạm trực tiếp four-eyes.

### CTV2-226 — CÒN SỐNG (retry storm đã giảm)

Mô tả gốc: `wake_coordinator` gọi coordinator chat/SSE nội bộ đã chết trong kiến
trúc MCP-native, từng ném configuration error và retry tới dead letter.

Bằng chứng code hiện tại:

- Actor vẫn tồn tại và chọn task/global `Session`, rồi gọi
  `CoordinatorService.run_turn_programmatic`
  (`backend/app/workers/agent_runner.py:92-146`). Đây chính là coordinator nội bộ
  mà hard boundary hiện tại đã thay bằng CLI ngoài repo.
- Driver vẫn enqueue wake khi có decision event (`agent_runner.py:286-303`).
- Phần “retry đến chết” không còn đúng: actor hiện `max_retries=0` (`:92-95`) và
  khi không có session trả `parked` (`:131-132`). Vì vậy blast radius đã giảm,
  nhưng với active legacy session nó vẫn chạy dead path và có thể claim event
  trước coordinator ngoài.

Cách tái hiện: tạo decision event + active global session thiếu model/agent rồi
gọi actor; đường `run_turn_programmatic` vẫn được thực thi. Trường hợp không có
session chỉ trả `parked`, xác nhận đây không phải event delivery cho MCP client.

Phạm vi: **nhỏ** nếu xóa actor/enqueue và dùng `wait_for_task`/TaskEvent hiện có;
**vừa** nếu cần notification transport mới cho coordinator ngoài.

### CTV2-230 — ĐÃ XONG

Mô tả gốc: orchestration driver đã tạo pending `review_order`, sau đó
`request_review` tạo pending thứ hai và để root đầu mồ côi.

Fix đã merge qua CTV2-1327:

- `5454890fcb7a80781d107d86f977696481d41ba1` thêm append-only stale cleanup;
  `508446b081b1f39fdc21ece133670e6e66d3f312` chuyển cleanup lên trước idempotency
  early-return; merge `62662a3bf04bb0d9bc2ded68d4149a1375874357` đã có verdict pass.
- Trước khi tạo gate supervised mới, `request_gate` gọi `_reject_pending_gates`
  cho cùng `gate_type` rồi commit (`backend/app/services/task_state_machine.py:
  820-848`). Cleanup không update/delete parent: nó insert child rejected qua
  `_reject_pending_gates` (`:284-319`), giữ đúng append-only ledger.
- Regression test `backend/tests/test_task_orchestration.py:138-163` chứng minh
  gate đầu có child `rejected/system:stale-cleanup` và chỉ gate thứ hai pending.

Implementation chọn “supersede append-only” thay vì reuse root cũ, nhưng invariant
quan trọng của task đã đạt: không còn hai pending childless cùng loại. Kết quả đo
lại sau restart lúc 04:38 ngày 2026-08-04 không còn duplicate pending cũng khớp
với code/test hiện tại.

### CTV2-231 — CÒN SỐNG

Mô tả gốc: review run bị watchdog/no-progress cancel nhưng task giữ `in-review`,
không có review failure và không quay lại `awaiting-review`.

CTV2-232 (`8a9c4a2`) thêm PID heartbeat và stream-json, làm false no-progress ít
xảy ra hơn; nó không sửa cancel transition:

- Brake `no_progress_limit` vẫn tồn tại ở
  `backend/app/services/task_validators.py:270-289`.
- Ở đầu worker, brake không queue chỉ ghi `run.status="cancelled"` rồi return,
  không gọi `record_review_failure`/`record_execution_failure`
  (`backend/app/workers/cli_executor.py:771-784`).
- `cancel_run` chỉ đưa task về `todo` khi task đang `dispatched`; review task
  `in-review` bị giữ nguyên (`backend/app/services/task_state_machine.py:
  1559-1597`).
- Chỉ cancellation nhận được như `ProcessResult` sau khi subprocess đã chạy mới
  đi qua nhánh review failure (`cli_executor.py:1173-1180`). Hai đường cancel
  trên bỏ qua nhánh này.

Dữ liệu CTV2-010 và CTV2-069 kẹt `in-review` hơn một tuần với 0 AgentRun và 0
pending gate chứng minh orphan state vẫn tồn tại, nhưng chưa đủ để gán chắc cho
watchdog. Reproducer xác định nguyên nhân của 231: tạo review run đang queued hoặc
running với task `in-review`, ép brake `no_progress_limit` (hoặc gọi `cancel_run`),
sau đó assert hiện tại run=`cancelled` nhưng task vẫn `in-review`; không có
`review_result/rejected` record.

Phạm vi: **vừa** — gom mọi terminalization của AgentRun vào một idempotent FSM
entry point phân nhánh execute/review, CAS task, append ledger và nudge driver;
test watchdog, explicit cancel, shutdown, dead-letter cho cả hai kind.

## Thứ tự và phụ thuộc của nhóm còn sống

1. **CTV2-225 trước hoặc song song với mọi giao bug**: critical, cần truy nguồn
   hai ca mới CTV2-232/1359 để biết còn đường nào bypass four-eyes. Không phụ
   thuộc code fix khác.
2. **CTV2-219 rồi CTV2-231**: 231 có thể sửa riêng, nhưng contract claim/attempt
   và idempotency từ 219 quyết định terminal callback được replay thế nào. Làm
   219 trước giảm khả năng tạo orphan mới trong lúc test 231.
3. **CTV2-220 và CTV2-223 cùng cụm eligibility**: 220 nên đóng agy fail-closed
   ngay; 223 tổng quát predicate reviewable/executable. Có thể một implementation
   dùng chung, nhưng phải giữ hai bộ AC (provider cwd và bad agent ids).
4. **CTV2-222 trước CTV2-224**: phải có một nguồn sự thật cho mode/autonomy trước
   khi mở tool đổi mode; nếu không gate mode-change sẽ có semantics mơ hồ.
5. **CTV2-218** và **CTV2-226** độc lập. Ưu tiên 218 sớm để giảm queue/log noise;
   226 là cleanup kiến trúc, không chặn FSM.

