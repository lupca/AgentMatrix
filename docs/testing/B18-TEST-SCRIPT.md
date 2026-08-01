# Kịch bản test B1.8 — Coordinator CLI điều phối trọn vòng đời qua native MCP

> Mục tiêu: chứng minh một phiên coordinator CLI (agy hoặc Claude Code) đi trọn todo→done không chạm REST, và các đường lỗi/bảo vệ hoạt động đúng. Tick từng ô; fail ở đâu ghi lại nguyên văn output ở đó.
>
> Chuẩn bị: backend + worker đang chạy (`/health` OK), token coordinator còn hạn trong config, DB còn seed `lt-proj` (dùng làm đạo cụ dọn dẹp ở P5).
>
> **Lưu ý setup quan trọng (rút từ lần test đầu):** chạy coordinator trong một **thư mục làm việc riêng** (vd `~/ct-coordinator/`), KHÔNG phải trong repo agenticmatix. Lần test đầu cho thấy khi thiếu tool tiện, agent sẽ tự đọc `.env` + mở DB trực tiếp bằng Bash — chạy ngoài repo để đường tắt đó không tồn tại, ép mọi thao tác đi qua MCP. Copy `.agents/mcp_config.json` sang thư mục đó (agy) hoặc `claude mcp add` (Claude Code).

## P0 — Khởi động & nhận diện

| # | Gõ cho coordinator | Kỳ vọng |
|---|---|---|
| 0.1 | `/mcp` (agy) hoặc `/mcp` (claude) | `control-tower` connected, không Unauthorized |
| 0.2 | "Liệt kê các tool control-tower bạn thấy" | ~22 tools, có `create_task`, `dispatch_task`, `approve_gate`, `get_task_events`, `suggest_agents` |
| 0.3 | "Có bao nhiêu project đang active?" | Trả lời từ `query_db entity=projects`, KHÔNG dùng Bash/đọc file |
| 0.4 | "Có bao nhiêu task? nhóm theo status" | `query_db` dùng tham số `sql`, trả về nhóm theo count, KHÔNG Bash/lật trang |

## P1 — Happy path trọn vòng đời (lõi của B1.8)

Dùng một project thật có `repo_root` hợp lệ (vd `agenticmatix` hoặc tạo project trỏ tới một repo test nhỏ).

| # | Gõ cho coordinator | Kỳ vọng |
|---|---|---|
| 1.1 | "Tạo task 'Thêm câu chào vào README' trong project <P>, acceptance criteria: README có dòng chào mới" | `create_task` ok, task id trả về, response có trường `next` gợi ý spec plan |
| 1.2 | "Chạy spec plan cho task đó" | `generate_spec_plan` ok; task có plan + AC |
| 1.3 | "Ai phù hợp làm task này?" | `suggest_agents` trả danh sách score + reason — advisory, KHÔNG dispatch |
| 1.4 | "Dispatch task (supervised)" | Gate pending; coordinator dừng lại **hỏi bạn** approve |
| 1.5 | Bạn trả lời "đồng ý" | `approve_gate` ok, response có `nudged: true`; task → dispatched |
| 1.6 | "Theo dõi tiến độ" | `get_task_events`/`get_status` thấy run chạy; executor spawn trong worktree |
| 1.7 | Chờ run xong | Task → awaiting-review (executor thật đã commit vào worktree) |
| 1.8 | "Cho review" | `request_review`: reviewer ≠ executor (four-eyes); nếu chỉ có 1 agent → fail rõ ràng, tạo thêm agent reviewer rồi thử lại |
| 1.9 | Approve review gate → chờ verdict | Review run chạy read-only git; verdict ghi nhận; task → done (hoặc changes-requested → lặp 1.4) |
| 1.10 | "Xem output của run vừa rồi" | `query_db entity=agent_runs` lấy run_id → `get_run_output` trả chunks — chuỗi 2 tool này phải tự nối được |

**Điều kiện đạt P1**: không một bước nào coordinator chạm REST/Bash-vào-DB; GateRecord có `approved_by` phản ánh human approve.

## P2 — Đường lỗi & guardrail

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| 2.1 | Dispatch lại task đang dispatched | Error có cấu trúc `task_transition_conflict` + hint; coordinator tự `get_status` rồi giải thích, không loop retry |
| 2.2 | Approve gate không tồn tại | `not_found`, message rõ |
| 2.3 | Mở phiên coordinator **thứ hai** (Claude Code), cả hai cùng dispatch một task todo | Đúng 1 thắng; kẻ thua nhận conflict + hint (CTV2-204 qua MCP) |
| 2.4 | Phát token executor scope task A (`issue-coordinator-token.sh executor <taskA>`), cấu hình client dùng nó, gọi tool trên task B | `task_scope_violation` |
| 2.5 | Cũng token executor đó, gọi `create_task` | `forbidden` — requires coordinator token |
| 2.6 | Phát token TTL 60s, chờ hết, gọi tool | 401/unauthorized ngay từ initialize hoặc tool call |

## P3 — Admin gates & api_key write-only

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| 3.1 | "Tạo agent reviewer mới chạy claude" (supervised) | `manage_agent` → pending admin gate → bạn approve bằng dạng `admin:<id>` → agent tạo xong |
| 3.2 | "Thêm api_key XXX cho agent đó, chuyển sang agent_type api provider openai" (bypass) | Thành công; response chỉ có `has_api_key: true`; **kiểm tra DB**: `admin_gate_records.input_payload` chứa `api_key_encrypted` (ciphertext), KHÔNG có chuỗi XXX plaintext |
| 3.3 | "Đổi setting max_concurrent_runs = 2" | `update_settings` qua gate; `query_db entity=settings` xác nhận |

## P4 — Sự kiện & quan sát

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| 4.1 | "Có gì mới từ lúc nãy?" (sau khi P1 chạy) | `get_task_events` với `since_id` cursor — chỉ event mới, không lặp |
| 4.2 | "Chi phí task vừa rồi?" | `get_stats` trả tokens + cost |
| 4.3 | "Đọc nội dung knowledge <id>" (tạo 1 knowledge trước bằng `manage_knowledge`) | `query_db` point lookup trả `content` đầy đủ |

## P5 — Dọn dẹp bằng chính tool (test `archive_task`)

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| 5.1 | "Archive toàn bộ task LT-xxx và project lt-proj" | `archive_task` × N + `manage_project` archive; `query_db` mặc định không còn thấy, `include_archived=true` thì thấy |

## Ghi nhận cải tiến (điền trong lúc test)

Phát hiện sẵn từ lần chạy đầu (chưa cần sửa ngay, ghi để thành task):

- [x] **`query_db` thiếu tổng số bản ghi** — agent phải lật trang mù (limit cap 50) rồi sinh ý định vượt rào. Đề xuất: thêm `total` (COUNT) vào response `query_db`, hoặc dạy qua tool description "dùng get_stats để đếm". (Đã giải quyết bằng query_db SQL v2)
- [x] **Coordinator trong repo CT tự đọc `.env`/DB khi bí** — không phải bug hệ thống, nhưng cần ghi vào `docs/coordinator-rules.md` + instructions: "mọi dữ liệu Control Tower phải lấy qua tool control-tower, không truy cập DB/file hệ thống trực tiếp"; về dài hạn cân nhắc chạy coordinator trong thư mục riêng như phần setup.
- [ ] **Không có tool đổi mode task** (phát hiện 2026-08-01, lần chạy agy đầu): "sửa mode thành tự động" không tool nào làm được → agy UPDATE thẳng DB, không gate, không audit. Fix: `update_task` nhận `mode` trong patch, đi qua admin/gate phù hợp.
- [ ] **Nghi bug result_ref ở đường bypass**: run thật vấp lỗi thiếu result_ref khiến review không nối được commit range; agy vá nóng `run.result_ref = f"{base_ref}.."` (đã revert, diff lưu tại `docs/agy-incident-2026-08-01.patch`). Dev điều tra chính chủ: vì sao result_ref không được ghi ở flow bypass, fix + test.
- [x] **ReviewResult schema quá strict so với artifact reviewer CLI viết thật — mismatch XÁC NHẬN là lỗi phía CT** (CTDE-001, reviewer @claude-fable ghi file thật): (a) prompt BẮT ghi `verdict` trong mỗi ac_results nhưng schema để verdict là property + `extra="forbid"` → reviewer làm đúng lời dặn vẫn fail validate; (b) prompt không nói `tests_run`/`tests_passed` là mảng chuỗi → reviewer ghi số đếm (int). Đã fix có chủ đích 2026-08-01: `verdict` thành field Literal thật (tự điền từ status nếu thiếu; mâu thuẫn với status thì reject — KHÔNG nới strict/extra), prompt reviewer nói rõ tests_* là arrays of strings. Verify bằng chính artifact fail + 3 biến thể. Dev bổ sung unit test cho ReviewACResult.
- [ ] **Task escalate để `awaiting_approval=true` + approval_prompt nhưng không có GateRecord pending** (CTDE-001: review-result invalid → task failed + awaiting_approval, coordinator gọi approve_gate theo prompt thì nhận "No pending gate found for task"): đường escalation cần hoặc tạo gate thật approve/reject được, hoặc đừng set awaiting_approval — nửa vời làm coordinator bị dẫn vào ngõ cụt.
- [x] **Luật "cấm sửa source/DB/process CT"**: đã thêm vào `docs/coordinator-rules.md` (mục Hard boundaries) và `SERVER_INSTRUCTIONS` trong `mcp_native.py` (kênh initialize, cả 3 CLI tự tiêm). Chốt chặn chính vẫn là chạy coordinator ở workdir riêng/project mục tiêu.
- [ ] **Zombie run `running` với PID chết không ai dọn** (2026-08-01, lần test P-00x): run MVA-017 kẹt `running` từ 30/07 (pid chết) chiếm 1/2 slot `max_concurrent_runs` → mọi dispatch sau bị brake QUEUE vĩnh viễn. `reconcile_orphaned_runs` chỉ quét `queued` + `dramatiq_message_id IS NULL`. Fix: reaper kiểm tra pid/heartbeat cho run `running` quá hạn, mark failed.
- [ ] **run_agent bị dead-letter thì run kẹt `queued` mãi mãi**: brake concurrency retry (30s) hết lượt → dramatiq đẩy message vào XQ, không ai xử lý XQ → AgentRun vẫn `queued` (có message_id nên reconcile bỏ qua), coordinator nhìn "queued" vô hạn. Fix: handler dead-letter mark run failed + error_message rõ (đã có nhánh dead-letter ở outbox — nối vào cả đường brake-retry).
- [ ] **Chuỗi self-reschedule của `outbox_publisher`/`reconcile` nhân bản qua mỗi lần worker chết/restart**: đo được 166+166 chuỗi trong DQ → hàng trăm execution/giây, log 15MB toàn WARNING. Fix: singleton scheduling (redis lock hoặc message_id cố định để dedupe), và tắt WARNING "returned a value" (return None hoặc thêm Results middleware).
- [ ] **Command builder cho agy build model/effort sai, 2 biến thể** (P-005 + P-007): (a) đường review dán effort vào tên model — `--model gemini-3.6-flash-medium` không có `--effort` → agy rơi vào chat-mode, không ghi `review-*.json`; (b) `--effort high` gửi cho model không hỗ trợ — `agy --model gemini-2.5-pro --effort high` → `Error: --effort is not supported for model "gemini-2.5-pro"`, fail cả 3 attempt → review_result rejected → task failed. Fix: một builder model/effort chung cho execute + review, biết model nào của agy nhận `--effort`, thêm test cho từng CLI × model.
- [ ] **`agent_events` seq đụng UniqueViolation làm retry lại run ĐÃ THÀNH CÔNG** (P-007 run 2c98351d): executor xong việc + commit (seq 7 `run.completed` 04:05:05), nhưng ghi event `llm.completed` seq 2 bị duplicate key → actor raise → dramatiq retry → attempt 3 chạy lại executor từ đầu (commit lần 2), rồi thành zombie `running` pid chết. Fix: seq phải cấp phát an toàn khi đua (lấy max(seq) FOR UPDATE hoặc sequence riêng per run), và event-write lỗi KHÔNG được làm fail/retry một run đã completed.
- [ ] **Run outlives task**: task P-007 đã `failed` (review rejected) nhưng execute run attempt 3 vẫn tiếp tục chạy nền — cần hủy các run đang hoạt động khi task chuyển sang trạng thái kết thúc (failed/cancelled).
- [ ] **`wake_coordinator` vẫn gọi `coordinator.complete_turn` (chat SSE cũ)** → `ConfigurationError: No model or agent is selected for this session`, retry đến chết. Thuộc nhóm dọn P1/P2 của GD4 (coordinator giờ là CLI ngoài, không cần wake qua LLM nội bộ).
- [x] **agy 1.1.9 nuốt prompt nếu có flag chen giữa `--print` và prompt** (CTDE-001, 2 executor agy trả lời lạc đề về flag): `--print --dangerously-skip-permissions 'prompt'` → mất prompt; `--dangerously-skip-permissions --print 'prompt'` → OK (verify thực nghiệm 3 thứ tự). Đã sửa `command_builder.py` (cả dispatch + review path). Dev bổ sung test cho command builder từng CLI.
- [x] **GỐC của chùm lỗi: run_agent bị giao 2 lần** (CTDE-001 run a4ef4fd1 + 63887436 — attempt 2 khởi động ~3s sau attempt 1 trên process khác): 3 call site sync trong `command_router` (`dispatch_task`, `request_review`, `approve_gate`) gọi `run_agent.send()` mà không ghi `run.dramatiq_message_id` → outbox publisher (guard dựa trên message_id NULL) tưởng chưa gửi → publish bản sao. Hai attempt song song sinh toàn bộ triệu chứng bên dưới. Đã fix: ghi message_id + commit ngay sau send ở cả 3 site (worker path `_enqueue_run` vốn đã đúng). Dev thêm test: dispatch qua MCP router → đúng 1 message, message_id được ghi.
- [ ] **Vệ sinh retry của `run_agent` — 4 lỗi hệ quả vẫn cần chống đỡ riêng** (vì duplicate/retry vẫn có thể xảy ra vì lý do khác):
  1. Retry chạy song song với attempt trước còn sống ("Refusing to kill stale PID: command does not match" rồi vẫn start attempt mới) → 2 process cùng ghi event → đụng seq UniqueViolation.
  2. Idempotency key `run:<id>:no-committed-changes` dùng chung cho MỌI lỗi execution (kể cả "Could not validate base ref") nhưng input chứa message khác nhau → `IdempotencyConflictError` unhandled → dramatiq retry tiếp → log ghi "attempt 4/3" vượt max_attempts.
  3. `WorktreeManager.remove` xóa worktree nhưng để lại branch `ct-run/<run-id>` → retry tạo worktree fail "branch already exists" → **âm thầm fallback về shared working tree**, phá worktree isolation; repo chính ct-demo bị bỏ lại checkout trên nhánh ct-run (phải dọn tay).
  4. Attempt sau xóa/đè worktree cùng đường dẫn (theo run-id) trong khi attempt trước đang dùng → git command fail giữa chừng → "Could not validate base ref for execution range" dù executor đã commit thành công (a533c44 tồn tại thật).
  Fix đề nghị: khóa per-run (không start attempt mới khi attempt cũ còn sống), idempotency key theo attempt hoặc input chỉ chứa mã lỗi, remove branch cùng worktree, và fallback shared-tree phải là lỗi cứng thay vì âm thầm.
- [x] **Verdict bị từ chối oan: "verdict requires a completed review run"** (CTDE-001/003 sau khi schema fix — vùng code lần đầu được chạy tới): `_submit_review_verdict` chạy trong lúc `run.status='success'` mới set in-memory; `SessionLocal` có `autoflush=False` nên query `_terminal_review_run` không thấy → PrerequisiteError → task failed dù review pass. Fix: `db.flush()` trước khi submit verdict (agent_runner). Sau fix: CTDE-001 + CTDE-003 đi trọn todo→done lần đầu tiên (2026-08-01 12:04).
- [ ] **agy headless (`--print`) KHÔNG làm việc trong cwd được spawn** (CTDE-009 run 42c41a66, executor @gemini-3.6-flash — root cause của mọi lỗi "result-ref outside range"/"no committed changes" với executor agy): output tự khai sửa `~/.gemini/antigravity-cli/scratch/README.md` (còn dính context "Marketing Video Agent" từ project cũ), commit trong scratch rồi báo hash — commit KHÔNG tồn tại trong repo task. Guard server chặn đúng. Executor/reviewer agy hiện không dùng được; dev research cơ chế workspace của agy headless (flag chỉ định workdir? cần register project?) trước khi bật lại. Nhân tiện: message lỗi nên tách "ref không tồn tại" khỏi "ngoài dải base..head".
- [ ] **Setting `default_mode` là nút chết**: nằm trong SETTINGS_WHITELIST với mô tả "Default gate mode for new tasks" nhưng không code nào đọc — coordinator đổi nó (admin:9) mà không có tác dụng. Mode thật quyết định bởi `mode_for_task`: setting `autonomy` (đang = "auto") + risk → task low-risk tự thành bypass. Fix: hoặc nối `default_mode` vào thật, hoặc bỏ khỏi whitelist; và tool description của `update_settings` nên nói rõ `autonomy` mới là nút chỉnh supervised/auto.
- [ ] **Reviewer tự động gán id lạ** (CTDE-003 done với "Reviewer: @user"; CTDE-001 với "@gpt-5.6-sol"): review-order tự chọn reviewer — kiểm tra pool agent nào được phép làm reviewer, "@user"/@lupca có phải agent CLI thật không hay là bản ghi rác trong bảng agents.
- [ ] (điền tiếp trong lúc test...)
