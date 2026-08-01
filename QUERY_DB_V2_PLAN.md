# Plan: query_db v2 — SQL có rào chắn thay cho filter DSL

> 2026-08-01. Dựa trên research có nguồn (dưới cùng). Thay thế đề xuất "thêm total/aggregate vào DSL" đã bàn trước đó — research chỉ ra đó là anti-pattern.

## Phát hiện chính từ research

1. **Agent xử lý SQL thuần tốt hơn DSL tự chế** — SQL nằm sâu trong training data (in-distribution); DSL tự chế là ngôn ngữ lạ agent phải học qua schema blob, và **mỗi operator còn thiếu là một lý do vượt rào mới**. Việc agy bỏ tool để mở DB trực tiếp bằng shell chính là hiện tượng kinh điển được ghi nhận trong tài liệu: "one flexible tool beats a hundred dedicated ones".
2. **An toàn không đến từ bó ngôn ngữ query mà từ bó môi trường thực thi** — pattern chuẩn ngành (postgres-mcp "restricted mode", Supabase read-only mode, Google genai-toolbox): role read-only + transaction read-only + AST validation + statement timeout + row cap. Nhiều lớp độc lập.
3. **Ít tool linh hoạt > nhiều tool hẹp**: registry tool phình to làm giảm đo được độ chính xác chọn tool (nghiên cứu RAG-MCP: 13.6%→43.1% khi thu nhỏ toolset). Không đẻ thêm 15 tool per-entity.
4. **Read-only KHÔNG đồng nghĩa an toàn tuyệt đối**: Supabase từng công bố tấn công exfiltration qua stored prompt injection chỉ với tool đọc. Với CT, nội dung do agent viết (task description, run output) chảy ngược qua tool này → allowlist phải nằm ở tầng GRANT, không phải tầng prompt.

## Thiết kế

### Tool `query_db` v2 — một param chính: `sql`

```json
{"sql": "SELECT project, count(*) FROM tasks WHERE status='dispatched' GROUP BY project"}
```

- Chỉ chấp nhận **một câu SELECT/WITH duy nhất**. COUNT, GROUP BY, JOIN, window function... miễn phí — không cần thiết kế pagination/aggregation API nữa, SQL chính là API đó.
- Giữ nguyên tên `query_db` (khỏi phình registry); param `entity`/`filters` cũ giữ tương thích 1 giai đoạn, description đánh dấu deprecated, gỡ ở Bước 3 dọn dẹp.

### 5 lớp phòng thủ (mỗi lớp độc lập, hỏng 1 còn 4)

| # | Lớp | Chi tiết |
|---|---|---|
| 1 | **Role Postgres `ct_readonly`** | Engine/URL thứ hai (SQLAlchemy) riêng cho tool này. `GRANT SELECT` theo bảng, và **theo cột** chỗ nhạy cảm: `agents` KHÔNG grant cột `api_key`; `sessions` KHÔNG grant cột `messages`; cân nhắc che `admin_gate_records.input_payload`/`output_payload`. Allowlist chuyển từ code Python xuống nơi nó được enforce thật. |
| 2 | **Transaction read-only** | `SET TRANSACTION READ ONLY` mỗi query + connect option read-only. |
| 3 | **AST validation bằng `pglast`** | Parser libpg_query — cùng parser của chính Postgres. Reject: mọi thứ không phải single SELECT/WITH; multi-statement; `COMMIT`/`ROLLBACK` (thoát được read-only transaction!); `SET`; `COPY`; function có side effect (`pg_sleep`, `dblink`...). Pattern đã proven trong crystaldba/postgres-mcp restricted mode. |
| 4 | **Giới hạn tài nguyên** | `statement_timeout=10s` per session; row cap 500 (bọc `SELECT * FROM (<sql>) q LIMIT 501` — dòng 501 chỉ để biết `truncated`). |
| 5 | **Audit log** | Ghi mọi câu SQL + session_id vào `AuditLog` (join key sẵn có). |

### Response envelope (theo guidance Anthropic)

```json
{"rows": [...], "row_count": 123, "truncated": true,
 "hint": "Kết quả bị cắt ở 500 dòng — thêm WHERE, hoặc dùng COUNT(*)/GROUP BY để tổng hợp thay vì lật trang."}
```
- Truncation message **dạy chiến lược** (aggregate thay vì paging) — đây là khuyến nghị tường minh của Anthropic.
- Cột lỗi trả actionable: lỗi pglast → "chỉ chấp nhận một câu SELECT duy nhất"; lỗi cột không tồn tại → kèm gợi ý "gọi với sql='SELECT ...' xem describe schema trong tool description".

### Schema đi kèm tool description (chống lỗi text2SQL số 1: mù schema)

- ~15 entity là đủ nhỏ để nhét **schema tóm tắt + enum giá trị** (status của task/run/gate, kind, role...) thẳng vào description của `query_db` (chú ý trần 2KB/description của Claude Code — nếu chật thì tách tool `describe_schema` read-only trả schema chi tiết).
- Theo guidance Anthropic: khuyến khích agent SELECT kèm cột ngữ nghĩa (title, name) bên cạnh id — "resolving UUIDs to semantically meaningful language significantly improves precision".

### Giữ các tool curated cho hot path (mô hình hybrid của genai-toolbox)

`get_status`, `get_stats`, `get_task_events`, `get_run_output`, `suggest_agents` **giữ nguyên** — đường nhanh, rẻ token, khỏi viết SQL cho việc lặp hằng ngày. SQL phủ long tail. Khi telemetry (`AuditLog` của lớp 5) cho thấy một dạng câu SQL lặp nhiều → cân nhắc thăng cấp thành tool curated (đúng mô hình `tools.yaml` của Google Toolbox).

## Việc cụ thể

1. **Migration/bootstrap role** — script SQL idempotent (chạy trong alembic migration hoặc `scripts/create-readonly-role.sh`): tạo `ct_readonly`, GRANT theo bảng + cột như bảng trên. `.env` thêm `DATABASE_URL_READONLY`.
2. **`app/db/base.py`**: engine + SessionLocal thứ hai từ `DATABASE_URL_READONLY` (pool nhỏ: 3+5). Fallback: nếu URL readonly chưa cấu hình → tool trả lỗi hướng dẫn chạy script, KHÔNG âm thầm dùng engine thường.
3. **`requirements.txt`**: thêm `pglast`.
4. **`app/services/sql_guard.py`** (mới): `validate_select(sql) -> str` — pglast parse, các rule lớp 3, trả SQL đã bọc row-cap. Đơn vị test dày ở đây.
5. **`command_router._handle_query_db`**: nhánh `sql` param → sql_guard → readonly session (`SET TRANSACTION READ ONLY`, `statement_timeout`) → envelope + audit. Nhánh `entity` cũ giữ nguyên, deprecated.
6. **`tool_registry`**: description mới (schema tóm tắt + enum + 2 ví dụ COUNT/GROUP BY + câu "không truy cập DB trực tiếp bằng cách khác").
7. **`docs/coordinator-rules.md` + instructions**: thêm luật "mọi dữ liệu CT lấy qua tool control-tower; cấm mở DB/đọc .env trực tiếp" (đóng mục cải tiến #2 của B18-TEST-SCRIPT).
8. **Tests** (`test_sql_guard.py` + mở rộng `test_gap_tools.py`):
   - Reject: UPDATE/INSERT/DELETE/DDL, multi-statement `;`, `COMMIT`, `SET`, `COPY`, `pg_sleep`, subquery chứa DML.
   - Grants (cần Postgres test hoặc đánh dấu integration): `SELECT api_key FROM agents` → permission denied; `SELECT messages FROM sessions` → denied.
   - Happy: COUNT, GROUP BY, JOIN tasks×agent_runs, ORDER BY LIMIT.
   - Envelope: truncated=true ở 501 dòng + hint; audit row được ghi.
   - **Bài học B1.7**: test grants phải chạy trên Postgres thật (SQLite không có GRANT) — thêm vào bộ integration chạy với `control_tower_db`.
9. **B18-TEST-SCRIPT.md**: thêm P0.4 "Có bao nhiêu task? nhóm theo status" — kỳ vọng 1 tool call SQL, không lật trang, không Bash.

## Rủi ro & đối sách

- **Prompt injection qua nội dung agent tự viết** (bài học Supabase): dữ liệu nhạy đã chặn ở GRANT; audit log để truy vết; không nối tool này với bất kỳ tool ghi nào trong cùng câu hướng dẫn.
- **SQLite trong test** không enforce GRANT → guard rule + envelope test bằng SQLite được, grant test bắt buộc Postgres (đã ghi ở mục 8).
- **Timeout/row-cap quá chặt cho câu phân tích nặng**: chỉnh được qua `SETTINGS_WHITELIST` (`sql_timeout_seconds`, `sql_row_cap`) — tái dùng cơ chế setting runtime sẵn có.

## Definition of Done

"Có bao nhiêu task, nhóm theo status, project nào tốn cost nhất tuần này?" — coordinator trả lời bằng 1–2 câu SQL qua `query_db`, không Bash, không lật trang; `SELECT api_key FROM agents` bị Postgres từ chối; toàn bộ test guard xanh; audit log có vết mọi câu SQL.

## Nguồn tham khảo

- Anthropic — Writing effective tools for agents: https://www.anthropic.com/engineering/writing-tools-for-agents (ít tool linh hoạt, response_format, truncation dạy chiến lược, natural-language identifiers)
- Google genai-toolbox (MCP Toolbox for Databases): https://github.com/googleapis/genai-toolbox (mô hình hybrid: tool tham số hóa cho production + execute_sql cho exploration)
- crystaldba/postgres-mcp restricted mode: https://github.com/crystaldba/postgres-mcp (pglast AST, chặn COMMIT/ROLLBACK escape, time limit)
- Supabase — Defense in Depth for MCP Servers: https://supabase.com/blog/defense-in-depth-mcp (read-only ≠ an toàn; stored prompt injection exfiltration)
- One Flexible Tool Beats a Hundred Dedicated Ones: https://towardsdatascience.com/one-flexible-tool-beats-a-hundred-dedicated-ones/
- Tool-count vs accuracy (RAG-MCP): https://arxiv.org/html/2605.24660v2
- Text-to-SQL security checklist: https://www.dpriver.com/blog/text-to-sql-security-10-risks-before-production-deployment/
- Keeping read-only really read-only: https://limerence.sh/blog/defense-in-depth-keeping-read-only-really-read-only
