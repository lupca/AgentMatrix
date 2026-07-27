# Autonomous Coordination — Gap Analysis

> Date: 2026-07-27 · Scope: luồng chat + context phân tầng
> Mục tiêu: user chat ở **global context** → điều phối **tự hoàn thành hết**, chỉ dừng ở gate hoặc khi cần xác nhận.
> Mục tiêu lớn: **giảm token** + **tăng chất lượng output phần mềm**.
> Đọc kèm: `docs/adr/ADR-001-unified-tool-architecture.md` (tool layer, đã triển khai Wave 1–3).

## 0. Kết luận ngắn

Flow đề xuất **đúng hướng và khả thi**, nhưng hệ hiện tại **chưa đáp ứng**. Kiến trúc đang thuần **reactive theo lượt chat**: mọi tiến triển chỉ xảy ra bên trong một turn LLM. Vòng đời task **đứt vật lý ở trạng thái `awaiting-review`** — không có tool nào để đi tiếp, và không có tiến trình nền nào đẩy task.

Ba thứ còn thiếu, theo thứ tự quan trọng:

1. **Orchestration Driver** — tiến trình nền event-driven đẩy task qua các bước máy móc với **0 token**.
2. **Spec/Plan Gate thật** — hiện task được tạo rỗng (không AC, không plan, không files/tests); đây là đòn bẩy chất lượng lớn nhất và đang trống hoàn toàn.
3. **Review Run thật** — không có gì spawn reviewer; `record_verdict` hiện cho phép coordinator tự ký verdict thay reviewer.

---

## 1. Hiện trạng vòng đời task (đã verify trong source)

```
create_task          → status=todo,  current_gate=spec     [command_router._handle_create_task]
dispatch_task        → GateRecord(dispatch)                [task_orchestration.request_dispatch]
  supervised → pending, task.awaiting_approval=True        → cần /approve
  bypass     → apply  → AgentRun queued → run_agent.send()
run_agent (dramatiq) → CLI chạy trong repo đích
  thành công → record_execution_success
               status=awaiting-review, current_gate=review_order, result_ref=git HEAD
  thất bại   → record_execution_failure → status=failed
──────────────────────── ĐỨT TẠI ĐÂY ────────────────────────
request_review       → status=in-review   ⚠ CHỈ gọi được qua REST POST /api/tasks/{id}/review
request_verdict      → status=done | changes-requested
```

### 1.1 Điểm đứt đã xác minh

`TOOL_REGISTRY` (tool_registry.py) có: `create_task`, `get_status`, `query_db`, `dispatch_task`, `record_verdict`, `approve_gate`, `cancel_task`, `compact_context`, `manage_project`, `manage_agent`, `manage_knowledge`, `update_task`, `load_tools`.

**Không có `request_review`.** Trong khi `request_verdict` bắt buộc `expected_status="in-review"`. Nghĩa là:

- Task xong execute → nằm `awaiting-review` **vĩnh viễn**.
- Coordinator không có công cụ nào chuyển nó sang `in-review`.
- `record_verdict` gọi vào sẽ luôn `TransitionConflictError: expected status 'in-review', found 'awaiting-review'`.

Chỉ có `POST /api/tasks/{id}/review` (api/tasks.py:186) — tức **user phải bấm nút trên UI**. Flow "tự hoàn thành hết" đứt ngay đoạn giữa.

### 1.2 Không có tiến trình nền nào

`grep` toàn backend: chỉ **một** dramatiq actor duy nhất — `run_agent`. Không có periodiq/APScheduler/cron/watcher. Khi `run_agent` kết thúc, nó cập nhật DB rồi im lặng. Không ai được đánh thức.

Coordinator thì bị giới hạn trong một turn: `max_tool_iterations = 5`, vượt là `RuntimeError` → persist failure. Một chuỗi tự chủ thực tế (`load_tools` → `create_task` → `update_task` (AC+plan) → `dispatch_task` → `get_status`) đã chạm trần ngay ở task đầu tiên.

---

## 2. Gap chi tiết

### G1 — Không có Orchestration Driver (chí mạng)

Mọi bước chuyển trạng thái đều phải do LLM chủ động gọi tool, trong khi phần lớn các bước là **thuần máy móc và tất định**: `awaiting-review` → chọn reviewer (≠ executor) → `request_review` → dispatch reviewer run → parse kết quả → `request_verdict`. Không có bước nào cần LLM. Ép LLM làm = vừa tốn token, vừa không chạy được khi không có ai đang chat.

### G2 — Spec/Plan Gate không tồn tại ở tầng dữ liệu (chí mạng cho chất lượng)

`_handle_create_task` tạo `Task(id, title, project, status='todo', current_gate='spec')`. **Không AC, không plan, không files, không tests.** `GATED_ACTIONS = {dispatch, review_order, verdict}` — spec/plan không phải gate thật.

LangGraph (`graph/nodes.py`) có `spec_gate`/`plan_gate` nhưng là **stub**: AC cứng `["AC1: Task defined", "AC2: Requirements parsed"]`, plan cứng `"1. Implement core features..."`, và `sync_to_db` chỉ `logger.info` — **không ghi DB**. Pipeline này là orphan: chỉ được `ContextHierarchy._graph_state_summary` đọc để hiển thị.

Hệ quả kép:
- Executor nhận task chỉ có title → chất lượng output phần mềm thấp. Đây chính là mục tiêu "tăng chất lượng task" đang bị bỏ trống.
- `_validate_verdict_prerequisites` dùng `required_count = max(1, len(task.acceptance_criteria or []))`. AC rỗng → chỉ cần **1** kết quả đánh giá là verdict `pass` hợp lệ → **cửa fake-done mở toang**.

### G3 — Không có Review Run thật; verdict tự ký

`_handle_verdict` truyền `actor=task.reviewer or f"chat:{session_id}"`. `_validate_verdict_prerequisites` chỉ kiểm tra `actor == task.reviewer`. Vì coordinator tự điền actor bằng chính `task.reviewer`, **LLM coordinator có thể ký verdict pass thay mặt reviewer** mà không có ai đọc diff. Four-eyes ở tầng DB (executor ≠ reviewer) vẫn "xanh" nhưng vô nghĩa về mặt thực chất.

Không có gì spawn reviewer agent chạy `/code-review` trong repo đích, không có gì sinh `ac_results` từ bằng chứng thật.

### G4 — `result_ref` có thể sai commit

`_parse_result_ref` chạy `git rev-parse HEAD` tại `repo_root` sau khi CLI kết thúc. Nếu executor **không commit**, giá trị trả về là commit cũ (thường chính là baseline). Review sẽ diff nhầm phạm vi và có thể pass một task chưa hề có code. Cần so `HEAD` trước/sau, hoặc bắt executor báo result-ref tường minh.

### G5 — Global chat không có project scope; `create_task` hardcode `project='default'`

- `_handle_create_task`: `project = 'default'` khi không có `--project`. `Task.project` là **FK tới `projects.id`** → nếu chưa có project id `default` thì `IntegrityError`; nếu có thì task rơi nhầm chỗ.
- Task ID sinh bằng `COUNT(*) + 1` → race condition khi tạo song song, và tái sử dụng ID sau khi xoá.
- Session global (`context_level='global'`) có `project_id=None` → `_scope_project_id` trả None → snapshot **không** liệt kê recent tasks. Đúng là ngữ cảnh mà user sẽ chat nhiều nhất lại là ngữ cảnh nghèo nhất.

### G6 — Không có chính sách tự chủ (autonomy policy)

`Task.mode` default `'supervised'` và **không có chỗ nào set nó khi tạo task**. Mọi dispatch/verdict đều dừng ở gate pending. Flow "tự chạy trừ khi cần xác nhận" cần policy theo project/risk (ví dụ: risk `low` → bypass, `high` → supervised), hiện chưa có. Cấu hình này thuộc về `Settings` KV (CTV2-083, đang dispatched).

### G7 — Không có đường quay lại sau thất bại

`failed` và `changes-requested` là ngõ cụt: không re-dispatch, không replan, không giới hạn vòng. Flow tự chủ bắt buộc phải có vòng lặp có chặn (ví dụ tối đa 3 vòng, sau đó escalate cho user).

### G8 — Không có kênh thông báo khi cần xác nhận

Gate pending chỉ set `task.awaiting_approval=True` + `approval_prompt`. Không có gì đẩy vào session chat global, không có WS/SSE notification. User không biết hệ đang chờ mình → "tự chạy" biến thành "đứng im không ai hay".

### G9 — Token: bốn rò rỉ cụ thể

1. **Vị trí snapshot phá cache history.** ADR-001 §D4 đã chuyển snapshot ra khỏi Tier-1 (tốt), nhưng đặt nó **trước** task/session history:
   `[global][project][snapshot][history]`. Snapshot đổi mỗi lần có mutation → **toàn bộ history phía sau rớt khỏi cached prefix**. History dài gấp nhiều lần snapshot, nên đây là khoản re-bill lớn nhất. Đúng phải là `[global][project][history][snapshot][user_msg]` — history là append-only nên tự nó là prefix ổn định.
2. **Coordinator chạy CLI = không có cache.** `_complete_cli` format **toàn bộ** history thành một prompt rồi spawn process mới mỗi turn. Không prefix caching, không tool loop (CLI path không nhận tool schema). Nếu coordinator chạy CLI thì mục tiêu giảm token thất bại về mặt cấu trúc.
3. **Tool result tích luỹ vĩnh viễn.** `_persist_tool_exchange` ghi mọi tool result JSON vào `session.messages`; `get_task_context` replay tất cả ở mọi turn sau. Một `get_status` trả 10 task sẽ bị tính tiền lại mãi mãi.
4. **Compaction phá thông tin.** `compact_context` giữ 10 message cuối + một dòng placeholder `[Context Compaction: Summarized N previous messages]` — **không tóm tắt gì cả**. Vừa mất context (giảm chất lượng), vừa kích hoạt quá muộn (threshold 50 message).

### G10 — Hai state machine song song

`TaskOrchestrationService` là FSM authoritative (có ledger, idempotency, khoá `with_for_update`). LangGraph là FSM thứ hai, stub, không ghi DB. Giữ cả hai = nợ kỹ thuật và nguy cơ lệch trạng thái.

---

## 3. Kiến trúc đề xuất

### 3.1 Orchestration Driver — trục chính

Một dramatiq actor `advance_task(task_id, trigger)` giữ vai trò "bánh đà". **Event-driven, không polling:**

```
run_agent xong            ─┐
gate được approve         ─┼─→ advance_task.send(task_id) ─→ đọc (status, mode, risk, round)
review run xong           ─┤                                  ─→ quyết định hành động kế
verdict = changes         ─┘                                  ─→ gọi TaskOrchestrationService
                                                              ─→ nếu còn việc: advance_task.send() lại
```

Bảng quyết định (thuần tất định, **0 token**):

| status | hành động kế | cần LLM? |
|---|---|---|
| `todo`, thiếu AC/plan | spec+plan step | ✅ 1 call |
| `todo`, đủ AC/plan | chọn executor (AgentMatcher) → `request_dispatch` | ❌ |
| `dispatched` | chờ `run_agent` | ❌ |
| `awaiting-review` | chọn reviewer ≠ executor → `request_review` → dispatch **review run** | ❌ |
| `in-review` | chờ review run → parse → `request_verdict` | ⚠ 1 call để tổng hợp ac_results nếu output không có cấu trúc |
| `changes-requested` | round < N → replan → re-dispatch; ngược lại escalate | ✅ 1 call |
| `failed` | retry hoặc escalate theo policy | ❌ |
| gate `pending` | dừng, thông báo user | ❌ |

Chỉ **2–3 LLM call cho cả vòng đời một task**, thay vì mỗi bước một lượt chat.

### 3.2 Spec/Plan step — đòn bẩy chất lượng

Một LLM call sinh đủ: `acceptance_criteria[]`, `plan`, `files[]`, `tests[]`, `risk`. Điểm mấu chốt: **`files`/`flows` phải lấy từ `code-review-graph` MCP** (`get_minimal_context_tool`, `get_impact_radius_tool`) thay vì để LLM đoán — đây đúng là bài học đã đúc kết ở control-tower v1 (AGENTS.md §2: AC + tests + files phải nguồn từ graph thật).

Kèm theo, siết `_validate_verdict_prerequisites`: **task không có AC thì không được vào dispatch**, và verdict `pass` phải có đủ số kết quả bằng số AC (bỏ `max(1, ...)`).

### 3.3 Review Run — biến review thành một loại run

Tái dùng nguyên `AgentRun` + `run_agent`, thêm `AgentRun.kind ∈ {execute, review}`:

- `request_review` (mới, thêm vào registry) chọn reviewer ≠ executor → tạo review run → CLI chạy `/code-review` trong repo đích với `--from <base> --to <result_ref>`.
- Output parse thành `ac_results` có cấu trúc; `record_verdict` được gọi với `actor = reviewer thật`, không phải coordinator.
- Coordinator **mất quyền** tự ký verdict: chặn `record_verdict` khi không có review run tương ứng (trừ khi user tự ký tay qua REST).

### 3.4 Autonomy policy

`Settings` (CTV2-083) + `Project`-level override:

```
autonomy: plan-only | supervised | auto
auto_max_risk: low | normal        # risk cao hơn luôn rơi về supervised
auto_max_rounds: 3                 # trần vòng changes-requested
```

Driver đọc policy để quyết định `Task.mode` khi tạo task, thay vì để mặc định `supervised` cứng.

### 3.5 Kênh xác nhận

Khi driver gặp gate pending hoặc cần user quyết định: ghi một message `role="system"` vào **session global của user** + phát WS event. Global chat trở thành **inbox điều phối**: user mở lên thấy "CTV2-090 đang chờ duyệt dispatch" và trả lời ngay trong luồng.

### 3.6 Context/token

| Vấn đề | Sửa |
|---|---|
| G9.1 snapshot giữa | Chuyển snapshot xuống **cuối**, ngay trước user message mới nhất |
| G9.2 CLI coordinator | Coordinator **chỉ API mode**; CLI dành riêng cho executor/reviewer run |
| G9.3 tool result tích luỹ | Chỉ replay tool result của **N turn gần nhất**; cũ hơn thì thay bằng 1 dòng tóm tắt |
| G9.4 compaction rỗng | Tóm tắt bằng LLM (model rẻ) khi vượt ngưỡng token (không phải đếm message), giữ lại quyết định/ID/ràng buộc |
| Session phình | **Sub-session per task**: driver chạy task trong session `context_level='task'` riêng; global session chỉ giữ 1 dòng kết quả mỗi task |

Sub-session là khoản tiết kiệm lớn nhất: công việc của 10 task không còn dồn vào một history duy nhất.

### 3.7 LangGraph

Chọn một trong hai, đừng giữ cả hai:

- **(a) Bỏ khỏi runtime** — `TaskOrchestrationService` đã là FSM đầy đủ có ledger; driver ở §3.1 chỉ là bảng quyết định mỏng. Đơn giản nhất, ít nợ nhất.
- **(b) Dùng thật** — cài lại nodes để gọi service (không tự set state), checkpointer Postgres làm nơi lưu vòng lặp replan. Có giá trị nếu sau này cần graph phức tạp (nhiều executor song song, sub-task).

Khuyến nghị **(a)** ở thời điểm hiện tại: giá trị LangGraph mang lại (checkpoint + resume) đã được `GateRecord` ledger phủ, trong khi chi phí là hai nguồn sự thật.

---

## 4. Lộ trình đề xuất

| # | Hạng mục | Vì sao trước/sau |
|---|---|---|
| 1 | `request_review` tool + Review Run (`AgentRun.kind`) | Nối lại chỗ đứt; không có nó thì không có gì "tự chạy" được |
| 2 | Orchestration Driver `advance_task` | Trục chính; bật autonomy cho toàn bộ vòng đời |
| 3 | Spec/Plan step + siết AC ở verdict | Đòn bẩy chất lượng; chặn fake-done |
| 4 | Fix `create_task` (project scope, ID sinh an toàn) + global session scope | Chặn lỗi vận hành ngay ở bước đầu tiên của flow |
| 5 | Autonomy policy (Settings + project override) | Cần cho "chỉ dừng khi cần" thay vì dừng ở mọi gate |
| 6 | Context/token: snapshot cuối, sub-session, tool-result pruning, LLM compaction | Mục tiêu token; làm sau khi flow đã chạy để đo được trước/sau |
| 7 | Notification/inbox trong global chat | Hoàn thiện trải nghiệm "dừng lại để xác nhận" |
| 8 | Quyết định LangGraph (bỏ hoặc dùng thật) | Dọn nợ, không chặn ai |
| 9 | `result_ref` chính xác (so HEAD trước/sau) | Nhỏ nhưng chặn một lớp verdict giả |

---

## 5. Đối chiếu report thứ hai (verify 2026-07-27)

Một bản report độc lập nêu thêm G11–G13 và mô tả chi tiết "status lock". Kết quả kiểm chứng trực tiếp trong source:

### 5.1 Review = dispatch với prompt khác — ĐÚNG, và FSM đang chặn đúng như mô tả

| Claim | Verdict | Bằng chứng |
|---|---|---|
| `request_dispatch` mặc định `expected_status="todo"` | ✅ đúng | `task_orchestration.py:92` |
| Gọi `dispatch_task` khi task ở `awaiting-review` → `TransitionConflictError` | ✅ đúng | `_assert_status` (`:848`) so sánh cứng |
| `request_dispatch` ghi `task.executor`, không phải `task.reviewer` | ✅ đúng | `_apply_gate` `:596` vs `:607` |
| `request_review` trả `None` cho AgentRun | ✅ đúng | `_apply_gate` `:612` `return None, task.result_ref` |
| `request_review` "sinh ra review_sheet" | ❌ **sai** | `generate_review_sheet` chỉ tồn tại ở `graph/gates/review.py` — module LangGraph orphan, `TaskOrchestrationService` không hề gọi. `request_review` chỉ set `reviewer`, `status='in-review'`, `current_gate='verdict'` |
| Không có tool `request_review` trong registry | ✅ đúng | registry hiện có 14 tool, không có mục nào |
| `POST /api/tasks/{id}/review` chỉ REST, LLM không gọi được | ✅ đúng | `api/tasks.py:186` |

Kết luận: nhận định "review chỉ là một agent run với prompt khác" **đúng về bản chất** và là hướng thiết kế nên theo (§3.3). FSM chặn ở ba lớp cùng lúc: khoá trạng thái, thiếu tạo `AgentRun`, thiếu tool.

### 5.2 G11 — Coordinator mù source code: ĐÚNG, và nặng hơn report mô tả

Registry không có tool đọc code — xác nhận. Nhưng vấn đề lớn hơn: `MCPClient` (`services/mcp.py`) và `GraphClient` (`services/graph_client.py`) **chỉ được export trong `services/__init__.py` và không được gọi từ bất kỳ đâu trong runtime path** — không coordinator, không command_router, không API.

Nghĩa là toàn bộ tích hợp `code-review-graph` (CTV2-005) và Headroom compression (CTV2-065) hiện là **dead code**. Hạ tầng để coordinator "nhìn thấy" code đã có sẵn, đã trả tiền để xây, nhưng chưa nối dây. Đây là chi phí thấp nhất để tăng chất lượng: thêm 1–2 tool (`get_minimal_context`, `get_impact_radius`) vào group `research` của registry là coordinator hết mù.

### 5.3 G12 — Idempotency: ĐÚNG, nhưng cơ chế hỏng khác với mô tả

`_command_key` = `sha256(args)[:24]` + session + action, **không nonce, không timestamp** (`command_router.py:830`). Retry với args y hệt cho key y hệt — đúng.

Nhưng hệ quả thực tế **nguy hiểm hơn** "trả về GateRecord cũ": trong `_request_gate`, `_idempotent_record` chạy **trước** `_assert_status` (dòng 18 vs 21 của hàm). Nên lần gọi lại:

1. Tìm thấy record cũ có cùng key + cùng `input_hash` → return ngay, **không kiểm tra trạng thái task hiện tại**.
2. Trả `applied=True` với `agent_run` = run **cũ** (đã failed/timeout).
3. Coordinator nhận tín hiệu "dispatch thành công" → **silent no-op**, không có run mới nào chạy.

Tức là không phải kẹt-báo-lỗi mà là **kẹt im lặng** — coordinator tin rằng việc đã được giao. Với flow tự chủ, đây là chế độ hỏng tệ nhất.

Sắc thái: nếu run cũ chưa terminal và `attempt < max_attempts`, `run_agent.send(run_cũ)` vẫn chạy lại được. Deadlock chắc chắn khi run cũ đã ở trạng thái terminal (`success`/`timeout`/`cancelled`, hoặc `failed` với `attempt >= max_attempts`) — `run_agent:114` sẽ discard message.

Sửa: đưa attempt/nonce vào key (`chat:{session}:dispatch:{hash}:{attempt}`), và/hoặc kiểm tra trạng thái trước khi trả record idempotent.

### 5.4 G13 — Không có DAG: ĐÚNG, không nhầm lẫn

Model `Task` (`db/models.py:43`) không có `parent_task_id`, không `depends_on`. `parent_id` xuất hiện ở `schemas/task.py:15,33` nhưng thuộc `GateRecordCreate`/`GateRecord` — đó là **chuỗi gate** (pending → decision), hoàn toàn không phải quan hệ giữa các task.

Hệ quả đúng như report: Epic → nhiều task thì thứ tự phụ thuộc chỉ nằm trong context window, mà context lại bị compact bằng cơ chế cắt cụt không tóm tắt (G9.4) → coordinator sẽ quên và dispatch loạn thứ tự. Cần `task_dependencies` (task_id, depends_on_task_id) + kiểm tra ở driver trước khi dispatch: chỉ dispatch khi mọi dependency đã `done`.

### 5.5 Cập nhật lộ trình

Ba hạng mục mới chèn vào bảng §4:

| # | Hạng mục | Ghi chú |
|---|---|---|
| 1b | Nới `expected_status` cho review dispatch + `AgentRun.kind` + `agent_role` (executor/reviewer) | Đi cùng hạng mục 1; không có nó thì review run không tạo được |
| 2b | Sửa idempotency key (thêm attempt/nonce) + kiểm tra trạng thái trước khi trả record | Chặn kẹt im lặng — **phải làm trước khi bật driver tự chủ**, nếu không lỗi này sẽ ẩn dưới automation |
| 3b | Nối `MCPClient`/`GraphClient` vào registry (group `research`) | Chi phí thấp nhất/đơn vị chất lượng: hạ tầng đã có sẵn, chỉ thiếu dây |
| 5b | Bảng `task_dependencies` + kiểm tra dependency trong driver | Cần cho Epic → sub-tasks; không có thì automation chạy sai thứ tự |
