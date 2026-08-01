# Plan: Chuyển Control Tower V2 sang Pure MCP Server

> Trạng thái: draft đã thống nhất qua bàn bạc (2026-08-01).
> Chiến lược: **strangler** — không đập đi xây lại. FastAPI chạy song song trong suốt quá trình chuyển, chỉ gỡ khi MCP server đã chứng minh ổn định.

## 1. Mục tiêu & bối cảnh

Loại bỏ dần lớp REST/WebSocket (frontend không còn cần thiết). Coordinator là **bất kỳ CLI agent nào** kết nối vào MCP server: Claude Code, Codex CLI, agy (Antigravity CLI)... Con người tương tác qua chat với coordinator, không qua UI.

Kiến trúc đích:

```
Database <-> CT MCP Server (streamable HTTP) <-> N coordinator CLIs
                 |                              + CLI executor agents (do worker spawn)
            Dramatiq Worker (giữ nguyên)
```

### Những gì KHÔNG đổi

- Toàn bộ `app/services/` (CommandRouter, TaskOrchestration, Coordinator, brakes, AgentMatcher...).
- Dramatiq worker, Redis broker, DB schema, GateRecord append-only, four-eyes constraint.
- Cơ chế concurrency đã có (xem §5).

## 2. Các quyết định đã chốt

| # | Quyết định | Lý do |
|---|---|---|
| 1 | MCP server mới chạy **streamable HTTP**, không stdio | N client đồng thời (nhiều coordinator + executor agents), 1 điểm enforcement chung |
| 2 | Tool handler gọi **thẳng in-process** `CommandRouter.execute_tool` (`command_router.py:237`), không forward qua REST như `mcp_server.py` hiện tại | Bỏ hop HTTP thừa nhưng giữ nguyên invariant: mọi enforcement (permission, gate, four-eyes) vẫn ở một chỗ duy nhất |
| 3 | **Token có role**: `coordinator` (dispatch, approve gate, xem mọi thứ) vs `executor` (báo cáo tiến độ, đọc context task của mình) | Identity nằm ở token, không nằm ở loại client — CLI nào cũng thay được |
| 4 | **Bỏ MCP Prompts và Resources** làm kênh workflow | Codex không hỗ trợ Prompts; Prompts ở client khác chỉ là slash command user gõ tay; Resources hỗ trợ không đồng đều |
| 5 | Kênh truyền workflow (theo độ tin cậy): (a) trường **`instructions`** khi initialize — cả Claude Code, Codex, agy đều tự tiêm vào system prompt; (b) **tool description** chứa precondition từng bước; (c) **tool result dẫn đường** — response kèm `next` gợi ý bước kế tiếp; (d) file instruction trong repo (gia cố) | Research đã verify (08/2026). Ràng buộc: instructions ≤ 2KB tổng (Claude Code cắt), 512 ký tự đầu tự đủ nghĩa (cửa sổ hiệu dụng của Codex) |
| 6 | File instruction generate từ **một nguồn duy nhất** ra: `CLAUDE.md` (Claude Code), `AGENTS.md` (Codex), `PROJECT.md` (agy — đọc PROJECT.md trước, fallback README). Không có GEMINI.md (Gemini CLI đã khai tử) | Tránh drift giữa 3 file |
| 7 | **Supervised mode**: human approve qua chat với coordinator → coordinator gọi `approve_gate`; GateRecord ghi `approved_by: human-via-<token>` | Không còn UI; giữ audit trail sạch |
| 8 | **Luật thật nằm server-side.** Tool gọi sai thứ tự / sai trạng thái bị từ chối kèm error message có tính hướng dẫn. Instructions/descriptions chỉ là UX | Agent "không biết" luật vẫn không bypass được |

## 3. Giai đoạn 1 — Dựng CT MCP Server native (song song FastAPI)

### 3.1. Server mới: `backend/app/mcp_native.py` (module riêng, không đụng `mcp_server.py` cũ)

- FastMCP app, transport streamable HTTP, mount port riêng (vd `:8100`) hoặc mount vào FastAPI app hiện có qua `/mcp` — chọn khi implement, ưu tiên process riêng để sau này gỡ FastAPI dễ.
- Nguồn tool duy nhất: `get_mcp_tool_specs()` (`tool_registry.py:605`) — vẫn là projection, không phải nguồn sự thật thứ hai.
- Handler: xác thực bearer token → resolve `(session_id, role)` → gọi `CommandRouter.execute_tool` in-process.

### 3.2. Auth & role

- Mở rộng scoped token hiện có: thêm claim `role` (`coordinator` | `executor`) và scope task (cho executor).
- Registry tool đánh dấu tool nào cần role nào; server từ chối trước khi chạm CommandRouter (defense in depth — CommandRouter vẫn kiểm lại).
- Lệnh/tool phát hành token coordinator cho người dùng (vd script `scripts/issue-coordinator-token.sh`).

### 3.3. Kênh workflow

- **`instructions`** (initialize): state machine task (todo→dispatched→awaiting-review→in-review→done), luật four-eyes, quy ước "đọc trường `next` trong mọi tool result". ≤ 2KB, 512 ký tự đầu là bản tóm tắt tự đủ.
- **Tool description**: mỗi tool ghi rõ precondition ("chỉ hợp lệ khi task ở `awaiting-review`; gọi `list_gates` nếu không chắc"). ≤ 2KB/tool.
- **Tool result dẫn đường**: chuẩn hóa envelope response `{ok, data, next?, error?}`. `next` là câu hướng dẫn bước kế tiếp do server sinh theo trạng thái thực của task.
- **Error có cấu trúc**: map `TransitionConflictError` → `{error: {code: "task_already_dispatched", by: "...", hint: "Gọi get_task để xem trạng thái mới."}}` thay vì chuỗi thô (đóng luôn gap #3 ở §5).

### 3.4. Instruction files

- Một nguồn: `docs/coordinator-rules.md` (tool nào có, triết lý four-eyes, "làm theo `next` trong tool result").
- Generator (mở rộng pattern của `cli_dispatcher.build_mcp_config`, `cli_dispatcher.py:171`) sinh/symlink ra `CLAUDE.md`, `AGENTS.md`, `PROJECT.md` cho workspace của coordinator.

### 3.5. Test

- Test MCP server in-process (fastmcp client test) phủ: auth role, gate flow qua tool, envelope `next`, error có cấu trúc, hai coordinator conflict (mượn kịch bản `test_command_router.py::test_concurrent_dispatch`).
- Giữ nguyên toàn bộ test services hiện có — không sửa services là tiêu chí của giai đoạn này.

**Definition of done GĐ1**: một phiên Claude Code kết nối `:8100` bằng token coordinator, đi trọn flow todo→done (kể cả supervised approve qua chat) mà không chạm REST API.

## 4. Giai đoạn 2 — Chuyển client sang server mới

1. Trỏ MCP config của CLI executor agents (`build_mcp_config`) sang server native (streamable HTTP thay stdio-forwarder), token role `executor`.
2. Chạy coordinator hằng ngày bằng Claude Code / Codex / agy trên server mới; FastAPI vẫn sống làm đường lùi.
3. Bổ sung dần những gì client thực sự hỗ trợ tốt (vd notifications) — không cam kết trước.
4. Sau vài tuần ổn định → quyết định số phận FastAPI + frontend (ngoài phạm vi plan này). Lúc đó `app/api/` gần như chỉ là vỏ để xóa; `mcp_server.py` (stdio forwarder) retire.

## 5. Concurrency — đã có sẵn, kèm backlog

Đã verify (CTV2-204, CTV2-088, CTV2-133): `Task.version` + CAS trong `_cas_status` (`task_orchestration.py:1978`), idempotency key + unique constraint trên GateRecord/AgentRun, `SELECT FOR UPDATE` trên Postgres, claim-event cho coordinator wakeup. Hai coordinator dispatch cùng task → kẻ đến sau nhận `TransitionConflictError`; `test_concurrent_dispatch` phủ đúng kịch bản. **Không xây mới.**

Backlog (không chặn GĐ1):

- [ ] **Supervised duplicate gate**: `_request_gate` (`task_orchestration.py:1218`) không qua CAS → hai coordinator tạo được hai pending gate cho một task (không chạy đôi, nhưng để gate mồ côi). Fix: CAS hoặc unique partial index trên pending gate per (task, gate_type).
- [ ] **Brake race**: `check_brakes` không race-safe giữa các task khác nhau khi `active=0` — hai spawn đồng thời có thể vượt `max_concurrent_runs`. Fix: advisory lock hoặc đếm bằng conditional UPDATE trên counter.
- [x] **Conflict error thô** → giải quyết trong GĐ1 (§3.3, error có cấu trúc).

## 6. Rủi ro & đường lùi

- Client hỗ trợ MCP không đều (notifications, resources) → thiết kế chỉ dựa vào tools + instructions, phần còn lại là bonus.
- FastAPI giữ nguyên đến hết GĐ2 → mọi thời điểm đều có đường lùi, không có giai đoạn hệ thống chết.
- `mcp_server.py` cũ giữ nguyên đến khi executor agents chuyển xong.
