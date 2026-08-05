# CTV2-1387 — Kiểm toán độc lập: cơ chế cốt lõi có THẬT SỰ chạy không

Ngày kiểm: 2026-08-05. Code đọc tại `a9bc82e` (nhánh `main`, worktree audit
riêng). Model kiểm toán khác họ với các agent đã viết phần lớn code này (chủ
đích của điều phối, xem mô tả task).

**Đây chỉ là báo cáo.** Không sửa code/test/migration/spec_item/dữ liệu DB nào.
Không tạo `knowledge_item`. File này là thay đổi duy nhất trong worktree.

## Nguyên tắc và giới hạn phương pháp

Mọi kết luận dưới đây dựa trên truy vấn `query_db` tự chạy lại (trích nguyên
văn), không dựa vào tên hàm/docstring/test xanh. Với các cơ chế mà bằng chứng
kích hoạt thật chỉ tồn tại trong **log ứng dụng runtime** (không phải DB) —
`/tmp/ct-mcp-1353.log` trong worktree này chỉ có 5 dòng, không phải log tiến
trình backend/worker thật đang chạy trên máy chủ, nên với các mục cần bằng
chứng "log ứng dụng" tôi không có kênh truy cập — điều này được ghi rõ là
**giới hạn bằng chứng cố hữu**, không suy diễn thành ĐÃ CHẠY hay KHÔNG CHẠY ĐƯỢC.

Một ngoại lệ: với `tool_argument_validator`, tôi tái hiện được trực tiếp bằng
cách tự gọi một tool MCP thật (`get_status`) với một tham số không khai báo,
ngay trong phiên audit này — đọc-only, không ghi DB, không đổi trạng thái task
nào. Đây là bằng chứng ĐÃ CHẠY mạnh nhất có thể có: quan sát trực tiếp hành vi
production, không suy từ dữ liệu lịch sử.

---

## Tóm tắt xếp hạng (mức nghiêm trọng nếu im lặng, cao → thấp)

| # | Cơ chế | Kết luận | Thay đổi so với 3 số đầu bài? |
|---|---|---|---|
| 1 | `failure_category` (CTV2-1368) | **KHÔNG CHẠY ĐƯỢC** (hiệu quả) | Số liệu mới: 1/95 tổng, nhưng 1/32 (không phải 1/1) trên riêng dữ liệu **sau** cutoff |
| 2 | Neo spec + phát hiện lệch | **ĐÃ CHẠY** (từ 3bca751, ~13h trước) | 0/260 xác nhận đúng ĐẾN TRƯỚC bản vá; sau vá đã chạy thật trên commit thật |
| 3 | Kiểm tham số tool | **ĐÃ CHẠY** (từ 0b6d082, ~4h trước) | 0/34 đúng đến trước bản vá; tôi tự tái hiện chặn thật ngay trong audit này |
| 4 | agy plan-critic parsing | **ĐÃ CHẠY** (từ 0f6eb91) | Task nói 0/14 — số thật trong chính commit fix là **0/12**, không phải 14 |
| 5 | GateRecord/AdminGateRecord append-only | **KHÔNG KẾT LUẬN ĐƯỢC** (có 1 khoảng hở thật) | — |
| 6 | Cô lập worktree | **CHƯA GẶP TÌNH HUỐNG** (đúng như thiết kế, có giới hạn đã biết) | — |
| 7 | Outbox dead-letter + thu dọn run mồ côi | **CHƯA GẶP TÌNH HUỐNG** | — |
| 8 | Nén ngữ cảnh / khoá idempotency / CAS-version | **CHƯA GẶP TÌNH HUỐNG** (nén) — nhưng CAS **ĐÃ CHẠY** rõ ràng bên trong cùng mục | — |
| 9 | Four-eyes (executor≠reviewer, planner≠critic) | **ĐÃ CHẠY** (ở lớp application, không phải DB constraint) | — |
| 10 | Phanh tự trị (cost/token/concurrency/no-progress) | **ĐÃ CHẠY** | Bác bỏ nghi ngờ `MAX_NO_PROGRESS_SECONDS` chết — đã kích hoạt 2 lần thật |
| 11 | notification_deliveries | **ĐÃ CHẠY** | Task nói 5 failed — số thật là **12** failed, không phải 5 |

---

## 1. `failure_category` — phân loại nguyên nhân thất bại (CTV2-1368)

**Kết luận: KHÔNG CHẠY ĐƯỢC (hiệu quả)** — code được gọi thật trên dữ liệu
thật, nhưng danh sách marker không khớp hình dạng lỗi thật nên vẫn rơi về
`unknown` gần như toàn bộ.

Truy vấn tách theo `LEGACY_CUTOFF = 2026-08-04T00:00:00+00:00`
(`backend/app/services/agent_run_classification.py:13`):

```sql
SELECT count(*) AS total_failed,
       count(*) FILTER (WHERE created_at >= '2026-08-04T00:00:00+00:00') AS failed_after_cutoff,
       count(*) FILTER (WHERE created_at >= '2026-08-04T00:00:00+00:00'
                         AND (failure_category IS NULL OR failure_category='unknown')) AS unknown_after_cutoff
FROM agent_runs WHERE status IN ('failed','timeout')
```
→ `{"total_failed":95,"failed_after_cutoff":32,"unknown_after_cutoff":31}`

Và tách theo giai đoạn:
```sql
SELECT (created_at < '2026-08-04T00:00:00+00:00') AS is_legacy_period,
       failure_category, failure_data_quality, count(*)
FROM agent_runs WHERE status IN ('failed','timeout') GROUP BY 1,2,3
```
→ 63 run **legacy** (trước cutoff, `failure_data_quality='legacy'`, toàn bộ
`unknown` — đúng như kỳ vọng, dữ liệu cũ trước khi có cột này) + 32 run
**current** (sau cutoff): 31 vẫn `unknown`, chỉ 1 là `agent_no_output`.

Con số gốc trong mô tả task (1/95) **đánh lận** hai nguyên nhân khác nhau:
94/95 "chưa phân loại" thật ra là 63 record cũ (đúng, hợp lý — cột này chưa
tồn tại) cộng với 31 record **mới, có phân loại chạy thật, vẫn ra unknown**.
Tách ra: tỷ lệ hữu ích trên dữ liệu hiện tại là **1/32 (3,1%)** — gần như
không đổi so với 1/95, chứng minh đây không phải "dữ liệu cũ chưa migrate"
mà là **bộ marker không khớp lỗi thật, đang tiếp diễn**.

Bằng chứng gốc (`classify_termination` được gọi tại
`task_state_machine.py:1472,1522,1615` và `cli_executor.py:833,1000,1423`,
xác nhận có wiring thật, không phải code chết):

```sql
SELECT id, task_id, error_message, failure_category, created_at
FROM agent_runs WHERE status IN ('failed','timeout')
  AND created_at >= '2026-08-04T00:00:00+00:00'
  AND (failure_category IS NULL OR failure_category='unknown')
ORDER BY created_at LIMIT 15
```
Vài dòng thật: `"Extra data: line 2 column 1 (char 1261)"`,
`"Expecting value: line 1 column 1 (char 0)"`,
`"3 validation errors for SpecPlanResult..."`,
`"Agent completed without committed changes; escalating for review"`,
`"Review result file is missing"` — tất cả đều là `unknown`.

Đối chiếu với marker list thật trong
`agent_run_classification.py:87-100` (nhóm `infra_parse`): chỉ chấp nhận
`"invalid json"`, `"malformed json"`, `"could not parse"`, `"parse error"`,
`"invalid review result"`, `"review result file"`,
`"unexpected end of json"`, `"jsondecodeerror"`. Chuỗi lỗi thật của Python
`json.JSONDecodeError` là `"Extra data: ..."` / `"Expecting value: ..."` —
**không chuỗi nào trong marker list khớp dạng lỗi thật này**, dù
`"review result file"` (khớp `"Review result file is missing"`) và
`"without committed changes"` (khớp dòng "Agent completed without committed
changes") **đều có trong marker list** nhưng hai dòng đó vẫn bị gắn `unknown`
trong DB — nghĩa là có một lớp bất nhất thứ hai (worker chạy code cũ hơn
code trên đĩa hiện tại, hoặc `failure_category` bị ghi trước khi các
marker này tồn tại) mà tôi không xác định dứt điểm được vì không có log
runtime để biết thời điểm worker restart. Dù nguyên nhân chính xác là gì,
kết luận thực nghiệm không đổi: **31/32 run thất bại mới nhất vẫn unknown**.

**Nếu ngừng hoạt động có ai biết không?** Không. Không có alert/threshold nào
theo dõi tỷ lệ `unknown`; `failure_report()` (agent_run_classification.py:167)
chỉ được gọi khi ai đó chủ động truy vấn. Chính task CTV2-1368 sinh ra từ việc
ai đó tình cờ soi số 1/95 — nếu không có audit này, tỷ lệ 31/32 (96,9%) trên
dữ liệu mới sẽ tiếp tục bị coi là "dữ liệu cũ" thay vì "bộ marker sai".

---

## 2. Neo spec + phát hiện lệch

**Kết luận: ĐÃ CHẠY** — kể từ bản vá `3bca751` (2026-08-05 09:42:26 +0700).
Trước đó xác nhận đúng 0/260 như mô tả task.

Xác minh độc lập số liệu gốc (không dùng lại số trong đề bài):
```sql
SELECT status, count(*) FROM spec_item GROUP BY status
```
→ `{"superseded":2},{"active":86},{"draft":172}` → **tổng 260**, khớp con số
"0/260" trong đầu bài (không phải 862 — 862 là tổng `spec_anchor`, một bảng
khác; xác nhận riêng: `SELECT count(*) FROM spec_anchor` → **841**, gần nhưng
không khớp 862 — chênh lệch nhỏ có thể do vài anchor bị xoá/thêm từ lúc viết
đề bài đến lúc audit).

```sql
SELECT count(*) FILTER (WHERE stale_reason IS NOT NULL) AS ever_had_reason,
       count(*) AS total FROM spec_item
```
→ `{"ever_had_reason":0,"total":260}` — xác nhận **0/260 CHƯA TỪNG** có
`stale_reason` — đúng số gốc, tính đến thời điểm audit.

Nhưng đây là số **hiện tại**, không phải bằng chứng bản vá còn hỏng. Kiểm
tra xem `apply_commit_staleness` (được gọi từ outbox event
`graph_rebuild_requested`, `outbox.py:207`) có thực sự chạy trên các commit
**sau** bản vá không:
```sql
SELECT id, payload, dead_letter, attempts, created_at
FROM outbox_events WHERE event_type='graph_rebuild_requested'
  AND payload::text ILIKE '%agenticmatix%'
ORDER BY created_at DESC LIMIT 8
```
→ 8 event thật, tất cả `dead_letter:false`, `attempts:1` (thành công lần
đầu), trải từ `2026-08-04T14:34` đến `2026-08-05T03:54` — nhiều event có
`commit_sha` nằm **sau** `3bca751` (vd. `"0bc74b65ed7d..a9bc82ef0d2d"`,
`"d9c36249af61..c94d88947384"`). Cơ chế được gọi thật trên landing thật,
không phải code chết.

Kiểm một trường hợp cụ thể để xác nhận độ chính xác chứ không chỉ "có chạy":
commit `c94d889` (trong range đã xử lý) sửa
`backend/app/services/task_state_machine.py` — file này **có neo** cho dự án
agenticmatix (10 symbol: `cas_status`, `apply_gate`, `transition_to_done`,
`_reject_pending_gates`, v.v — `SELECT sa.path, sa.symbol, si.status FROM
spec_anchor sa JOIN spec_item si ON sa.spec_item_id=si.id WHERE
si.project_id='agenticmatix' AND sa.path='backend/.../task_state_machine.py'`
→ 10 dòng). Diff thật của `c94d889` trên file này (`git diff
d9c36249af61..c94d88947384 -- .../task_state_machine.py`) chỉ thêm hàm MỚI
`record_plan_critic_verdict` — **không đụng đến 10 symbol đã neo**. Vậy việc
`stale_reason` vẫn NULL cho các spec_item này là **đúng** (true negative),
không phải cơ chế im lặng bỏ sót.

Điều chưa xác nhận được: một ca **dương tính thật** (một commit thật sự sửa
một symbol đã neo, và `stale_reason` được ghi) — bản vá mới chạy ~13 giờ,
chưa có landing nào chạm đúng một trong các symbol hẹp đã neo cho dự án này.
Kết luận ĐÃ CHẠY dựa trên: (a) code được gọi thật với input thật liên tục,
(b) nhánh so khớp/không-lệch cho ra kết quả đúng trên ít nhất một ca kiểm
tay — không dựa trên suy diễn từ test.

**Nếu ngừng hoạt động có ai biết không?** Không có alert. `outbox_events`
ghi lại đã xử lý (attempts, dead_letter) nên VỀ NGUYÊN TẮC có thể dò được qua
`query_db`, nhưng không ai chủ động theo dõi bảng này định kỳ — phát hiện lại
sẽ giống hệt cách bug gốc bị phát hiện: tình cờ, khi điều tra việc khác.

---

## 3. Kiểm tham số tool (`tool_argument_validator`)

**Kết luận: ĐÃ CHẠY** — kể từ `0b6d082` (2026-08-05 07:37:53 +0700), và tôi
**tự tái hiện được ngay trong audit này**.

Xác nhận wiring thật (không chỉ định nghĩa hàm mà không ai gọi):
```
grep -n "validate_tool_arguments" backend/app/mcp_native.py
→ mcp_native.py:398: problems = validate_tool_arguments(spec, kwargs)
```
gọi cho **mọi** tool, trước khi mở DB session (`mcp_native.py:388-411`, thứ
tự: authn/authz → validate arguments → DB session).

Bằng chứng ĐÃ CHẠY mạnh nhất: tôi tự gọi tool MCP thật
`mcp__control-tower__get_status` với một tham số không khai báo
(`bogus_extra_field_for_audit_test`), ngay trong phiên audit này (đọc-only,
không mở DB session vì bị chặn trước bước đó):

```
Input: {"task_id":"CTV2-1387","bogus_extra_field_for_audit_test":true}
Output: {"ok":false,"error":{"code":"unknown_arguments",
  "message":"get_status: 'bogus_extra_field_for_audit_test' is not a
  parameter of this tool and was NOT saved", ...}}
```
Đây là quan sát trực tiếp hành vi production ngay lúc audit — không phải suy
từ dữ liệu lịch sử hay test. Xác nhận số 34 tool: `grep -c "ToolSpec("
backend/app/services/tool_registry.py` → **34**, khớp mô tả task.

Không tìm được kênh DB lưu vết các lần chặn LỊCH SỬ trước lần tự-test này:
hàm trả lỗi thẳng cho caller trước khi mở DB session nên không ghi
`audit_log`/`tool_metrics` nào (`tool_metrics` chỉ phủ 7 tool graph/ocr/review,
không phải toàn bộ 34 tool MCP). Vì vậy không đếm được đã chặn bao nhiêu lần
kể từ 07:37 hôm nay ngoài lần tự-test — nhưng KHÔNG cần suy diễn: chỉ cần MỘT
lần quan sát thật là đủ điều kiện ĐÃ CHẠY theo tiêu chí đề bài.

**Nếu ngừng hoạt động có ai biết không?** Không có kênh giám sát chủ động
(không log, không metric). Đây chính xác là kiểu thất bại im lặng mà cơ chế
này được sinh ra để chặn — nhưng bản thân việc NÓ ngừng hoạt động lại không
có gì phát hiện, ngoại trừ tình cờ như lần phát hiện gốc (audit dữ liệu
`raw_input` rỗng).

---

## 4. agy — bóc tách output / plan-critic parsing

**Kết luận: ĐÃ CHẠY** (từ `0f6eb91`) cho nhánh **plan-critic**; nhánh
**review-verdict** (JSON file review kết quả) chưa bao giờ hỏng vì nó không
phụ thuộc CLI nào — cần tách hai nhánh vì đề bài gộp chung.

**Đối chiếu số "0/14" trong đề bài:** không khớp bất kỳ số nào tôi tìm được
trong DB hay code. Số thật, trích nguyên văn từ chính commit fix
`0f6eb91` (`git show 0f6eb91`):
> "Số liệu 2 ngày: plan_critic trên agy 12 run, 0 thành công. Cùng vai trò đó
> trên claude: 12/12 thành công"

→ **0/12, không phải 0/14**. Đây là phát hiện độc lập của audit này: số
trong mô tả task lệch so với nguồn nó tự trích dẫn.

Nguyên nhân gốc (trích code fix, `cli_provider.py`): agy lồng câu trả lời
xuống một tầng (`{"event":"result","result":{"response":"..."}}`), hàm
`_extract_cli_text` cũ chỉ khớp khi `result` là CHUỖI nên rơi xuống trả
nguyên `stdout` (JSONL thô) cho caller — planner parse nhầm dòng envelope
đầu tiên, biểu hiện `"Extra data: line 2 column 1"`.

Kiểm tra sau bản vá bằng `gate_records` (bảng ghi verdict `plan_critic` thật,
`gate_type='plan_critic'`):
```sql
SELECT id, task_id, actor, status, created_at
FROM gate_records WHERE gate_type='plan_critic' AND actor ILIKE '%antigravity%'
```
→ 3 dòng: `1320` (`@antigravity-3.6-medium`, approved,
`2026-08-04T10:30:03Z` — **trước** giờ vá UTC ~15:50, một ca lạ chưa giải
thích được, xem dưới), `1454` (`@antigravity-3.6-medium`, approved,
`2026-08-04T23:05:24Z` — sau vá), `1472` (`@antigravity-3.6-high`, rejected
với lý do cụ thể "Plan rejected due to fabricated file paths...", cũng sau
vá). Hai ca sau vá là bằng chứng ĐÃ CHẠY sạch: agy tạo verdict hợp lệ, kể cả
verdict "rejected" có nội dung thật (không phải lỗi parse trá hình thành
approve).

Ca `1320` (trước giờ vá) là một điểm bất thường thật: nó thành công dù bản vá
chưa lên main lúc đó. Có thể do biến thể model (`medium` thay vì `high`) tình
cờ trả JSON không lồng tầng, hoặc do một fix trung gian khác chưa xác định.
Tôi không suy diễn thêm — ghi lại như một điểm chưa giải thích được, không
làm thay đổi kết luận tổng ("agy plan critic từng chết 0/12, nay đã chạy
được ít nhất trên các ca sau giờ vá").

Nhánh review-verdict (khác plan-critic): đọc `cli_executor.py:1292-1311` +
`output_parser.py:82-164` — parser đọc file JSON cố định
(`.ct/review-<task_id>.json`), **không có nhánh riêng theo CLI nào** (không
`if cli == "agy"`), nên không thể "hỏng vì là agy". Xác nhận bằng dữ liệu
thật:
```sql
SELECT ar.agent_role, ar.status, count(*) FROM agent_runs ar
WHERE ar.cli='agy' GROUP BY ar.agent_role, ar.status
```
→ reviewer: `success=54, failed=16, cancelled=3, running=1`. 54 review agy
đã tạo verdict đọc được thành công — nhánh này **ĐÃ CHẠY** từ lâu, độc lập
với bug plan-critic.

**Nếu ngừng hoạt động có ai biết không?** Có, nhưng chậm: lỗi rơi vào
`agent_runs.error_message` (chuỗi Pydantic/json lỗi thật) và
`gate_records`/task escalation — nhìn thấy được qua `query_db`, nhưng không
ai chủ động rà theo CLI. Bug gốc bị phát hiện vì task timeout hết hạn (chuỗi
retry ăn hết 300s), không phải vì có giám sát riêng cho agy.

---

## 5. GateRecord / AdminGateRecord — append-only

**Kết luận: KHÔNG KẾT LUẬN ĐƯỢC** cho "đã từng chặn thật chưa" (không thể —
xem lý do), nhưng phát hiện được **một khoảng hở thật** đáng ghi nhận riêng.

`gate_records` có **cả hai lớp**: Postgres `TRIGGER trg_gate_records_immutable`
thật (`backend/alembic/versions/007_gate_system_consolidation.py:127-142`)
CỘNG với guard ORM Python (`before_update`/`before_delete`,
`backend/app/db/models.py:589-596`). Nhưng grep toàn bộ `backend/app` cho
`.update(` / `db.delete(`/`session.delete(` kết hợp `GateRecord` → **0 kết
quả** — không có bất kỳ đường code nào trong ứng dụng từng thử UPDATE/DELETE
một `GateRecord`. Kết luận: "chưa từng quan sát nó chặn" ở đây có nghĩa
**"chưa từng có cơ hội chặn"**, không phải "có cơ hội mà không chặn được".
Đây là bằng chứng NEGATIVE hợp lệ (structurally unreachable), không phải
KHÔNG KẾT LUẬN ĐƯỢC.

Cái thật sự KHÔNG KẾT LUẬN ĐƯỢC và đáng lo hơn: **`admin_gate_records`
không có trigger DB tương đương** — grep toàn bộ alembic versions
(001, 007, 015_admin_gate_records.py, 016) cho `CREATE TRIGGER ... ON
admin_gate_records` → 0 kết quả. Chỉ có guard ORM Python
(`models.py:636-643`), **có thể bị bỏ qua bởi bất kỳ client SQL nào không đi
qua ORM này** (một migration thủ công, một script vá dữ liệu, một kết nối
psql trực tiếp). Vì đây là DB production thật dùng chung cho nhiều dự án
(voma, topvnsport, agenticmatix — 11 project_id khác nhau trong `spec_item`),
nguy cơ một script vận hành khác chạm `admin_gate_records` qua đường không
phải ORM là có thật, dù chưa có bằng chứng nó ĐàXẢY RA.

**Nếu ngừng hoạt động có ai biết không?** Với `gate_records`: có, DB sẽ ném
lỗi cứng (`IntegrityError`) nếu ai đó thử — nhưng vì chưa ai từng thử, không
có ví dụ lỗi thật để xác nhận thông điệp lỗi đúng như kỳ vọng. Với
`admin_gate_records`: **không** — một UPDATE/DELETE trực tiếp SQL sẽ đi qua
êm, không log, không alert.

---

## 6. Cô lập worktree

**Kết luận: CHƯA GẶP TÌNH HUỐNG** đối với phần đã cô lập thật (working
tree/branch); phần **không** cô lập (object database) là **thiết kế có chủ
đích, đã tài liệu hoá**, không phải bug — nhưng tên gọi "isolation" gây hiểu
lầm phạm vi, và đã từng gây sự cố thật (inbox `1c0201fa`, theo đề bài).

Xác nhận dùng `git worktree add`, không phải `git clone`
(`backend/app/services/process_manager.py:345-347`) — 0 kết quả grep cho
`git clone` trong toàn bộ `backend/app`.

Docstring thừa nhận thẳng, trích nguyên văn `process_manager.py:296-313`:
> "Because a worktree shares the parent repo's object database, any commit
> made on `ct-run/<run_id>` is immediately reachable from `repo_root`
> (`git log --all`, `git show <sha>`, ...) even after the worktree directory
> itself is removed -- no merge is required for the commit to be durable."

**Cái ĐƯỢC cô lập:** thư mục working tree riêng, git index riêng, HEAD riêng,
branch riêng (`ct-run/<run_id>`) — hai run song song không đụng
`.git/index.lock` của nhau và không di chuyển HEAD của nhau.
**Cái KHÔNG được cô lập:** git object database (`.git/objects` dùng chung) —
commit của một worktree LUÔN thấy được từ mọi worktree khác và từ
`repo_root` ngay lập tức. Đây chính là cơ chế khiến một migration từ nhánh
chưa duyệt (theo đề bài, inbox `1c0201fa`) có thể đổi schema DB thật: object
database dùng chung không ngăn việc *đọc* code từ nhánh chưa merge, kể cả khi
mỗi worktree có working tree riêng.

**Nếu ngừng hoạt động có ai biết không?** Có một phần: nếu `git worktree add`
thất bại, `WorktreeUnsupportedError` được raise và caller "fail closed"
(không âm thầm rơi về chia sẻ repo — theo audit trước, CTV2-1369, mục
CTV2-220 ghi nhận **có** đường fallback về repo dùng chung ở
`cli_executor.py:847-862`, trái hard boundary — vẫn CÒN SỐNG tính đến audit
trước, tôi không kiểm lại độc lập ở đây vì ngoài phạm vi 10 mục). Với việc
object database dùng chung: không có cảnh báo nào, vì đó là hành vi được
thiết kế, không phải lỗi.

---

## 7. Outbox dead-letter + thu dọn run mồ côi

**Kết luận: CHƯA GẶP TÌNH HUỐNG** cho cả hai — code đúng, có wiring thật, chỉ
là điều kiện kích hoạt chưa xảy ra trên dữ liệu hiện có.

Dead-letter (`OutboxEvent.dead_letter`, cần đủ 5 lần thử thất bại —
`MAX_PUBLISH_ATTEMPTS=5`, `outbox.py:32-34`):
```sql
SELECT count(*) FILTER (WHERE dead_letter=true) AS dead_lettered,
       count(*) AS total
FROM outbox_events
```
→ `{"dead_lettered":0,"total":410}`.
```sql
SELECT event_type, attempts, dead_letter, count(*) FROM outbox_events
GROUP BY event_type, attempts, dead_letter
```
→ số lần thử cao nhất từng thấy là **2** (`graph_rebuild_requested`), cách xa
ngưỡng 5 — chưa từng gần tới điều kiện dead-letter, không phải "hỏng, không
kích hoạt được".

Thu dọn run mồ côi (`reconcile_orphaned_runs` / `reap_dead_running_runs`,
`outbox.py:297-423`, tự lập lịch lại mỗi 60s qua actor Dramatiq
`outbox_publisher.py:52-77`, khởi động cùng worker boot
`workers/__init__.py:41`): điều kiện kích hoạt là `AgentRun` bị kẹt
`queued` >60s không có message theo dõi, hoặc `running` với PID đã chết.
```sql
SELECT status, count(*), count(*) FILTER (WHERE pid IS NOT NULL) AS with_pid
FROM agent_runs GROUP BY status
```
→ `running: 3 (cả 3 đều có pid)`, không có `queued` mồ côi hiện tại. Không có
cột nào trong `agent_runs`/`audit_log` ghi lại việc reconciliation từng phục
hồi/reap một run trong quá khứ (hành động của nó là sửa `status` tại chỗ,
không để lại dấu "đã từng mồ côi" sau khi sửa) — nên không đếm lại được số
lần lịch sử qua `query_db`; log ứng dụng (`outbox.py:364`, `:463-465`) là nơi
duy nhất ghi sự kiện này và tôi không có quyền truy cập log runtime thật.

**Nếu ngừng hoạt động có ai biết không?** Không có alert chủ động. Nếu actor
tự lập lịch bị hỏng (crash loop, hoặc bootstrap không gọi), triệu chứng duy
nhất là run kẹt `queued`/`running` mãi mãi — chỉ lộ ra khi ai đó tình cờ nhìn
`agent_runs` bằng mắt hoặc task báo timeout ở tầng trên.

---

## 8. Nén ngữ cảnh / khoá idempotency / CAS-version trên task

**Kết luận: CHƯA GẶP TÌNH HUỐNG** (nhãn tổng của mục — lấy theo phần yếu nhất
trong ba, vì đó là phần đáng lo nhất). Ba cơ chế khác mức trưởng thành rõ
rệt — gộp một mục theo đúng phạm vi đề bài nhưng tách kết luận từng phần
dưới đây: 8a CHƯA GẶP TÌNH HUỐNG, 8b KHÔNG KẾT LUẬN ĐƯỢC, 8c ĐÃ CHẠY.

### 8a. Nén ngữ cảnh (`compact_context`) — **CHƯA GẶP TÌNH HUỐNG**

Kích hoạt tự động khi token vượt 75% cửa sổ model
(`context_hierarchy.py:537-568`, `COMPACTION_THRESHOLD_RATIO=0.75`), gọi
KHÔNG điều kiện trước mỗi lượt LLM thật (`coordinator.py:1002` trong
`complete_turn`, `coordinator.py:1257` trong `stream_turn`) — không phải chỉ
tool MCP thủ công.
```sql
SELECT count(*) FROM llm_usage WHERE operation='compaction'
```
→ **0**.
```sql
SELECT count(*) FROM sessions WHERE messages::text LIKE '%msg-compact-%'
```
→ **0**. Cả hai kênh bằng chứng (usage row `operation='compaction'`, marker
`msg-compact-<session_id>` trong `sessions.messages`) đều rỗng — chưa từng có
session nào thật sự vượt 75% cửa sổ token trong toàn bộ lịch sử dữ liệu hiện
có. Wiring đúng, chỉ là chưa gặp phiên hội thoại đủ dài.

### 8b. Khoá idempotency — **KHÔNG KẾT LUẬN ĐƯỢC** (cho va chạm thật)

Có 2 lớp: application-level (`idempotent_record`,
`task_validators.py:522-540`, so khớp key rồi so `input_hash`) VÀ ràng buộc
DB thật (`uq_gate_records_task_idempotency` — `models.py:535-539`,
`uq_agent_runs_task_idempotency` — `models.py:974-977`). Lớp DB độc lập với
logic ứng dụng — hai request đồng thời cùng key sẽ tự va vào unique
constraint. Không có cách truy vấn DB để biết một collision từng xảy ra
trong quá khứ (row bị từ chối không để lại dấu vết, chỉ request thắng mới có
row) — cần log ứng dụng (`IntegrityError`/`IdempotencyConflictError`) mà tôi
không truy cập được. Đã có MỘT sự cố sống được biết đến (đề bài không yêu
cầu điều tra lại: đổi reviewer giữa vòng đụng key `advance:CTV2-232:review:r2`,
ghi trong `docs/reviews/CTV2-1369-TASK-AUDIT.md` mục CTV2-219) — xác nhận cơ
chế phát hiện xung đột **đã từng nổ ra thật**, dù không phải qua audit này.

### 8c. CAS/version trên task — **ĐÃ CHẠY**

```sql
SELECT version, count(*) FROM tasks GROUP BY version ORDER BY version
```
→ 332 task ở version 0, **~215 task ở version ≥1** (dải đến 36). Cột
`version` chỉ tăng qua `cas_status()` (`task_state_machine.py:145-164`,
điều kiện `WHERE status=expected AND version=expected_version`,
`rowcount != 1` → `TransitionConflictError`) — hàng trăm lượt tăng version
thật chứng minh **CAS chạy trên mọi transition thật của hệ**, không phải code
chết. Việc CAS từng **bắt được một xung đột thật** (hai request đua nhau,
`rowcount=0`) riêng thì không đếm lại được qua DB (route lỗi không lưu vết
lâu dài), cùng giới hạn bằng chứng như 8b.

**Nếu ngừng hoạt động có ai biết không?** Nén: không — session sẽ chỉ phình
context và tốn token/thất bại ở lớp khác (context window vượt hạn mức của
model) mà không có tín hiệu riêng cho "nén đã tắt". Idempotency: có, nhưng
gián tiếp — task escalate lỗi hiển thị trong `query_db`/`get_task_events`,
như sự cố CTV2-232 đã lộ. CAS: có, cực rõ — `TransitionConflictError` chặn
đường ngay, task không bao giờ âm thầm mất transition.

---

## 9. Four-eyes (executor≠reviewer, planner≠plan_critic)

**Kết luận: ĐÃ CHẠY** — nhưng ở lớp application, không phải DB constraint.

Xác nhận `planner`/`plan_critic` là **cặp cột riêng thật**, không phải alias
của executor/reviewer: constraint `ck_tasks_plan_four_eyes`
(`models.py:274-278`), có `require_independent` riêng
(`task_state_machine.py:1941`) và check riêng trong
`spec_plan_generator.py:669-670`.

Tầng chặn thật nằm ở `TaskValidator.require_independent`
(`task_validators.py:598-608`, raise `PrerequisiteError("Reviewer must
differ from executor")`), được gọi TRƯỚC mọi điểm ghi executor/reviewer vào
DB (`task_state_machine.py:219,703,757,1099,1165,1185,1941`,
`task_validators.py:445`). Không tìm được đường code nào có thể tạo ra một
cặp executor=reviewer chạm tới DB write mà không đi qua check này trước —
nghĩa là **constraint DB (`ck_tasks_four_eyes`, `ck_gate_records_four_eyes`)
là lớp phòng thủ thứ hai không có cơ hội được kích hoạt**, vì lớp application
luôn chặn trước. "Chưa từng thấy DB constraint tự nổ" ở đây = "chưa từng có
cơ hội", không phải khiếm khuyết.

Tôi xếp nhãn ĐÃ CHẠY (không phải KHÔNG KẾT LUẬN ĐƯỢC) vì bằng chứng ở đây
khác mục 5 (GateRecord) và mục 6 (worktree): `require_independent` **chính
là cơ chế four-eyes** theo đúng nghĩa vận hành (chặn một request thật trước
khi nó thành vi phạm), và nó chắc chắn đã chạy — mọi `record_verdict`/
`request_review` thành công trong hệ (hàng trăm task `done`) đều đã đi qua
đường này với input hợp lệ. Tách bạch: DB CheckConstraint (backstop) chưa
từng nổ; nhưng "four-eyes" như một cơ chế đúng nghĩa (ngăn vi phạm xảy ra)
đã hoạt động liên tục.

**Nếu ngừng hoạt động có ai biết không?** Nếu `require_independent` bị xoá/
lỗi mà DB constraint còn nguyên: mọi lần vi phạm sẽ ném `IntegrityError` ngay
khi transaction commit — task đó fail ồn ào, dễ thấy. Nếu CẢ HAI cùng hỏng
(vd. constraint bị drop nhầm trong một migration): không có tín hiệu nào
khác cảnh báo, đúng như sự cố "drift main-vs-ledger" mà audit trước
(CTV2-1369, mục CTV2-225) đã ghi nhận có bằng chứng sống.

---

## 10. Phanh tự trị (cost / token / concurrency / no-progress)

**Kết luận: ĐÃ CHẠY** — bằng chứng mạnh nhất trong toàn bộ 11 mục, bác bỏ
trực tiếp nghi ngờ của đề bài về `MAX_NO_PROGRESS_SECONDS`.

```sql
SELECT action, details->>'code' AS code, count(*)
FROM audit_log WHERE action LIKE 'brake:%' GROUP BY action, details->>'code'
ORDER BY count(*) DESC
```
→ `concurrency_limit: 132`, `pending_gate: 17`, `dependency_pending: 12`,
`agent_capability: 3`, `terminal: 3`, **`no_progress_limit: 2`**,
**`token_limit: 2`**, **`cost_limit: 1`**.

Chi tiết `no_progress_limit` (2 lần, cả hai trên `CTV2-227`,
`2026-08-01`), trích `details.decision.observations` nguyên văn:
`no_progress_seconds: 949` và `959`, so với `max_no_progress_seconds: 300`
(ngưỡng LÚC ĐÓ) — brake kích hoạt đúng lúc vượt ngưỡng thật, không phải giả
lập. **Lưu ý quan trọng**: ngưỡng hiện tại đã nâng lên
`SELECT value FROM settings WHERE key='max_no_progress_seconds'` → **2400**
(gấp 8 lần) — brake chưa kích hoạt lần nào kể từ khi ngưỡng được nới, nên
"đã từng chạy" là đúng cho ngưỡng 300s cũ, còn hành vi ở ngưỡng 2400s hiện tại
là **CHƯA GẶP TÌNH HUỐNG** riêng (chưa có run nào im lặng đủ 40 phút để thử
lại).

`token_limit` (2 lần, `CTV2-1340`: 25.135.878 ≥ 20.000.000; `CTV2-1371`:
20.702.858 ≥ 20.000.000) và `cost_limit` (1 lần, `VOMA-033`: $17.49 ≥ $10.00)
đều có `gate_record_id` thật đi kèm trong `transition:safety_brake:rejected`
(`audit_log` id 2824, 3480, 3883) — mỗi lần brake nổ đều tạo `GateRecord`
`gate_type=safety_brake` thật, task chuyển `failed` thật, không phải chỉ ghi
log suông.

**Nếu ngừng hoạt động có ai biết không?** Có, khá tốt: mỗi lần brake nổ ghi
`audit_log` (`brake:<code>`) VÀ tạo `GateRecord`/chuyển task `failed` — cả
hai đều truy vấn được qua `query_db`/`get_status`. Đây là cơ chế có "tiếng
kêu" tốt nhất trong 11 mục, đối lập hẳn với mục 1 (failure_category) và mục 3
(trước khi vá).

---

## 11. notification_deliveries — đối chiếu số coordinator đã cho

**Kết luận: ĐÃ CHẠY** — cơ chế ồn ào nhất trong 11 mục (thất bại có ghi log
`last_error` rõ ràng), dùng làm mẫu đối chứng phương pháp cho 3 mục im lặng
đầu bài.

```sql
SELECT status, attempts, last_error, count(*)
FROM notification_deliveries GROUP BY status, attempts, last_error
ORDER BY status
```
→ `{"status":"failed","attempts":3,"last_error":"HTTP 403","count":12}`,
`{"status":"sent","attempts":1,"last_error":null,"count":1}`,
`{"status":"skipped","attempts":0,"last_error":null,"count":578}`.

Đối chiếu với số coordinator đưa trong đề bài (578 skipped / **5** failed /
1 sent): **skipped và sent khớp đúng (578, 1)**, nhưng **failed thật là 12,
không phải 5**. Đây là phát hiện độc lập — không điều tra lại NGUYÊN NHÂN
(theo đúng giới hạn đề bài, đã giải ở nơi khác), chỉ đối chiếu số. Chênh
lệch 5→12 có thể do dữ liệu tiếp tục phát sinh giữa lúc đề bài được viết và
lúc audit chạy (task này bản thân cũng đang chạy qua hệ thống — thấy trong
`agent_runs` các dòng `CTV2-1387` review), hoặc do đề bài trích sai — không
kết luận thêm vì ngoài phạm vi yêu cầu.

Đối lập rõ với 3 mục im lặng đầu bài: notification_deliveries có
`last_error` cụ thể ("HTTP 403") ngay trên từng row thất bại — bất kỳ ai
`query_db` một câu đơn giản cũng thấy ngay có vấn đề, đúng là cơ chế "kêu to"
tự nhiên nhờ ghi lỗi tại chỗ ghi trạng thái, không phải một hệ thống giám sát
riêng.

**Nếu ngừng hoạt động có ai biết không?** Có — `last_error` nằm ngay trên
mỗi row `notification_deliveries`, không cần suy luận hay log ngoài DB.

---

## Nhận xét tổng kết — vì sao 3 số đầu bài đúng nhưng không đủ

Ba cơ chế bị đề bài nêu tên (neo spec, kiểm tham số, agy critic) đều có
đặc điểm chung: **lỗi nằm ở một điểm nối duy nhất giữa hai tầng** (repo path
vs project id; JSON Schema permissive default vs FastMCP; JSONL envelope vs
response text) — và cả ba đều **đã được vá trong đúng ngày audit này diễn
ra** (`3bca751`, `0b6d082`, `0f6eb91`), nên câu hỏi thật sự hữu ích không còn
là "có hỏng không" (có, đã xác nhận độc lập) mà là "bản vá có thật sự chạy
chưa" — và cả ba đều **ĐÃ CHẠY**, xác nhận bằng dữ liệu thật phát sinh sau
giờ vá, không phải suy diễn từ diff code.

Phát hiện mới, không nằm trong 3 số gốc: mục nghiêm trọng nhất trong báo cáo
này (`failure_category`, mục 1) **KHÔNG** nằm trong danh sách "đã có dấu
hiệu" của đề bài — nó chỉ lộ ra khi tách riêng dữ liệu theo `LEGACY_CUTOFF`,
đúng như đề bài yêu cầu làm ở bước phân loại nguyên nhân. Đây là bằng chứng
cho thấy phương pháp "tách trước/sau cutoff" tự nó có giá trị phát hiện, không
chỉ là thủ tục xác nhận.
