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

- [ ] **`query_db` thiếu tổng số bản ghi** — agent phải lật trang mù (limit cap 50) rồi sinh ý định vượt rào. Đề xuất: thêm `total` (COUNT) vào response `query_db`, hoặc dạy qua tool description "dùng get_stats để đếm".
- [ ] **Coordinator trong repo CT tự đọc `.env`/DB khi bí** — không phải bug hệ thống, nhưng cần ghi vào `docs/coordinator-rules.md` + instructions: "mọi dữ liệu Control Tower phải lấy qua tool control-tower, không truy cập DB/file hệ thống trực tiếp"; về dài hạn cân nhắc chạy coordinator trong thư mục riêng như phần setup.
- [ ] **Không có tool đổi mode task** (phát hiện 2026-08-01, lần chạy agy đầu): "sửa mode thành tự động" không tool nào làm được → agy UPDATE thẳng DB, không gate, không audit. Fix: `update_task` nhận `mode` trong patch, đi qua admin/gate phù hợp.
- [ ] **Nghi bug result_ref ở đường bypass**: run thật vấp lỗi thiếu result_ref khiến review không nối được commit range; agy vá nóng `run.result_ref = f"{base_ref}.."` (đã revert, diff lưu tại `docs/agy-incident-2026-08-01.patch`). Dev điều tra chính chủ: vì sao result_ref không được ghi ở flow bypass, fix + test.
- [ ] **ReviewResult schema có thể quá strict so với artifact reviewer CLI viết thật**: reviewer trả JSON fail validate (StrictStr/extra=forbid) → agy nới toàn bộ schema để cho qua (đã revert, cùng patch trên). Nếu mismatch là thật: sửa có chủ đích ở prompt reviewer hoặc schema kèm test — tuyệt đối không nới strict/extra.
- [x] **Luật "cấm sửa source/DB/process CT"**: đã thêm vào `docs/coordinator-rules.md` (mục Hard boundaries) và `SERVER_INSTRUCTIONS` trong `mcp_native.py` (kênh initialize, cả 3 CLI tự tiêm). Chốt chặn chính vẫn là chạy coordinator ở workdir riêng/project mục tiêu.
- [ ] (điền tiếp trong lúc test...)
