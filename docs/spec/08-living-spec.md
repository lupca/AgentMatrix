# 08 — Hệ spec sống

> **TRẠNG THÁI: MỘT PHẦN ĐÃ TRIỂN KHAI.** Đã có thật trong code (CTV2-1341,
> CTV2-1342, CTV2-1355, CTV2-1367, CTV2-1395): bảng
> `spec_item`/`spec_relation`/`spec_anchor`/`spec_task_link`/`impl_design`, cột
> `spec_item.stale_reason` và `spec_item.realization`, tool
> `spec_write`/`spec_get`/`spec_stale`, cơ chế mất hiệu lực thuần code ở mục
> *Cơ chế mất hiệu lực* dưới đây (`backend/app/services/spec_anchor.py`, hook
> vào `_publish_graph_rebuild` trong `backend/app/services/outbox.py`), và
> trục thực hoá thuần code ở mục *Trục THỰC HOÁ* dưới đây
> (`_realization_projection` trong `backend/app/services/spec_service.py`).
> Phần còn lại của file (spec_search, spec_link như tool riêng, pm_wbs_node,
> pm_document) **vẫn chỉ là thiết kế, chưa triển khai** — mọi bảng/tool có
> tiền tố đó khác các bảng liệt kê ở trên đều chưa tồn tại.
>
> Bản trước của file này tên `08-pm-layer.md`, xoay quanh WBS/lịch/critical
> path. Đã cắt phần lớn — xem mục *Đã cắt gì và vì sao* ở cuối.

## Vấn đề: tái suy diễn, không phải thiếu tài liệu

Triệu chứng người dùng gặp: mỗi khi có chức năng mới đều phải nhờ agent đọc spec
xem có trùng hay xung đột không. Thao tác này lặp lại liên tục trước khi ra được
task.

Nhưng triệu chứng nặng hơn nằm ở chỗ khác: **đọc lại nhiều lần cho ra kết luận
khác nhau.** Ví dụ có thật, ghi lại từ chính phiên thiết kế ra file này
(2026-08-04):

- Đọc lượt 1 → kết luận "tách 2 process". Đọc lượt 2 → tự sửa thành "2 endpoint
  trên 1 process".
- Khẳng định "`spec_clarity` là biến dự báo số vòng tốt nhất" → đo lại → **tự
  bác bỏ**.
- Khẳng định "`tasks.priority` chỉ cần kiểm chứng" → tra tiếp → hoá ra **không
  có hàng đợi nào cả**.

Cả ba lần đều đọc đúng file, không lần nào sai thao tác. Nguyên nhân là **đọc
tức là lấy mẫu**: mỗi lượt chạm vào một phần khác của cơ sở mã, nên ra kết luận
khác. Càng nhiều repo (hiện 12 app VOMA) thì phương sai càng lớn.

Chữa bằng "đọc kỹ hơn" hay "context lớn hơn" là chữa sai bệnh. Cách chữa đúng:

> **Ghi lại cái đã suy ra, kèm nguồn gốc và điều kiện hết hạn. Lần sau truy vấn,
> chỉ suy lại phần đã bị gắn cờ lỗi thời.**

Đây là điểm khác căn bản so với "kho tài liệu": tài liệu chỉ ghi *kết luận*, hệ
này ghi thêm *dẫn xuất từ đâu* và *khi nào thì không còn đúng nữa*.

## Số liệu nền (đo 2026-08-04, 57 task có run thật)

```
Số vòng execute/task            Thời lượng mỗi run
1 vòng  ██████████████  37      execute  median 6.1'  p90 10.2'
2 vòng  ███              8      review   median 5.0'  p90  9.4'
3 vòng  ███              7
5 vòng  █                1      trung bình 1.82 vòng/task
6 vòng  ██               3      65% one-shot, 35% phải làm lại
7 vòng  █                1
```

Thời lượng một run gần như **hằng số** (~6 phút); phương sai nằm ở **số vòng**.
Nên đơn vị ước lượng là **vòng**, không phải phút.

### Mô tả task đang quá mỏng

| Nhóm | Task | Vòng TB | Vòng max | Độ dài mô tả TB |
|---|---|---|---|---|
| 4–6 AC | 31 | **2.00** | 7 | **204 ký tự** |
| 7+ AC | 21 | 1.62 | 6 | 306 ký tự |
| 1–3 AC | 6 | 1.50 | 3 | 264 ký tự |

Nhiều AC hơn **không** làm tăng số vòng — ngược lại. Nhóm tệ nhất là nhóm **mô tả
ngắn nhất**. Mô tả trung bình 200–300 ký tự tức 2–3 câu; với công việc không tầm
thường thì đó là đặc tả thiếu. Bằng chứng yếu (n nhỏ, có nhiễu) nhưng không mâu
thuẫn giả thuyết "đặc tả kỹ thì ít làm lại".

### Hai cảnh báo phải giữ

**`spec_clarity` chưa chứng minh được là giảm số vòng.** Đo: nhóm `high` trung
bình 2.27 vòng (n=11) so với nhóm chưa set 1.72 vòng (n=46) — **ngược chiều** giả
định. Nhiều khả năng do thiên lệch chọn mẫu (`generate_spec_plan` chỉ chạy cho
task vốn đã khó), nhưng **không được dùng số này biện minh cho việc xây tầng
mới**. Bối cảnh: **381/391 task chưa từng set `spec_clarity`** — cơ chế chốt chất
lượng sẵn có gần như không ai bật.

**Chi phí chưa đo được — nhưng đo ĐƯỢC, chỉ là chưa nối dây.** `llm_usage` có 158
bản ghi, `agent_run_id` và `task_id` **NULL toàn bộ**; `_task_cost()`
(`task_validators.py:336`) join đúng hai cột NULL đó nên **luôn trả 0** ⇒ brake
`max_cost_usd_per_task` **chưa từng kích hoạt**, dù CLAUDE.md mô tả là có.
Attribution đã sửa ở CTV2-1338.

> **ĐÍNH CHÍNH (2026-08-04).** Bản trước của file này khẳng định "agent CLI chạy
> subscription nên không báo cost theo token". **SAI** — đó là suy diễn, không
> phải đo. Thử thật thì `--output-format json` cho ra đầy đủ:
>
> | CLI | input/output tokens | cache | cost USD |
> |---|---|---|---|
> | claude | ✅ | ✅ creation + read | ✅ **có sẵn** |
> | qwen | ✅ | ✅ read | ❌ (phải tự tính từ bảng giá) |
> | agy | ✅ (+ thinking) | ✅ read | ❌ |
> | codex | ✅ (+ reasoning_output) | ✅ cached_input | ❌ (chưa quy đổi) |
>
> Đo lúc 2026-08-04: `claude -p "say ok" --output-format json` → `in:2 out:4
> cache_read:15273 cost_usd:0.1366`. `agy --output-format json` → `in:18397
> out:35 thinking:28`. `codex exec --json` → `in:15134 cached_input:9984 out:5 reasoning_output:0`. Nối dây token: **CTV2-1350** (claude/qwen/agy) & **CTV2-1360** (codex).
>
> Bài học lặp lại lần thứ tư trong cùng phiên: **đừng suy từ mô hình kinh doanh
> (subscription ⇒ không có token) — hãy gõ lệnh thử `--help`.**

Sau khi CTV2-1350 và CTV2-1360 hoàn tất, cả 4 CLI (claude, qwen, agy, codex) đều đã được nối dây đo token thành công trong `llm_usage`.

## Data model

### Lõi: mệnh đề spec có neo

```sql
spec_item (
  id, project_id,
  kind,              -- requirement | decision | constraint | interface | design
  title, body,
  status,            -- draft | active | stale | superseded
  stale_reason,      -- ghi bởi cơ chế mất hiệu lực: symbol nào, commit nào (CTV2-1342)
  supersedes_id,
  source_doc_id,     -- pm_document sinh ra nó

  -- nguồn gốc: thứ làm nên khác biệt so với một kho tài liệu
  derived_from_sha,  -- commit lúc suy ra
  derived_by,        -- agent/người
  confidence,        -- asserted | derived | verified
  verified_at, verified_by,
  embedding,         -- pgvector, phục vụ tìm trùng
  archived_at
)

spec_anchor (
  spec_item_id,
  repo, path, symbol,   -- trỏ tới node của code graph
  relation,             -- implements | constrains | tests | documents
  anchor_sha            -- băm khai báo Python hoặc toàn bộ file lúc neo
)

spec_relation (
  from_id, to_id,
  kind                  -- conflicts_with | duplicates | refines | depends_on
)

spec_task_link (
  id, spec_item_id, task_id,
  relation,             -- implements | modifies | violates | references
  confidence,           -- asserted | derived | verified
  created_by, created_at,
  unique(spec_item_id, task_id, relation)
)
```

`spec_anchor` là bộ phận quyết định. Không có neo thì spec chỉ là văn bản và
không thể biết khi nào nó hết đúng.

`kind='decision'` chính là ADR. Hiện `docs/adr/` là file rời — chuyển vào đây thì
quyết định mới **neo được vào code**, tránh việc agent đời sau vô hiệu hoá một
quyết định có chủ đích vì không biết nó tồn tại. Đây là dạng lệch spec nguy hiểm
nhất vì nó âm thầm.

`spec_task_link` là cạnh nhiều-nhiều nối hai nửa project spec và task plan. Một
spec có thể được nhiều task hiện thực hoặc sửa qua thời gian; một task cũng có
thể chạm nhiều spec. Cạnh có thể được ghi **thủ công** qua op `"task_link"` của
`spec_write`; khi task land, hệ còn tự đối chiếu file trong reviewed diff với
`spec_anchor.path` cùng project/repo để ghi cạnh `modifies` có
`confidence=derived`, `created_by=system:landing`. Cơ chế này idempotent và
không dùng LLM; nó ghi lịch sử task-spec, còn việc đánh dấu stale vẫn do commit
invalidation so hash của symbol.
Chọn `spec_write` thay vì nhét danh sách vào `impl_design` vì `impl_design` là
bản hiện tại có thể được ghi lại, trong khi cạnh spec-task là lịch sử độc lập
cần tồn tại qua nhiều plan. `spec_get(task_id=...)` đọc từ task ra spec;
`spec_get(ids=[...])` trả `task_links` để đọc ngược từ spec ra task.

### Bản thiết kế thực thi

```sql
impl_design (
  id, task_id,
  summary,
  files jsonb,        -- [{path, action: create|modify|delete, why}]
  changes jsonb,      -- [{symbol, signature, behavior, edge_cases}]
  data_changes jsonb, -- migration, đổi schema
  test_plan jsonb,
  risks jsonb,
  derived_from_sha,
  authored_by,        -- model mạnh
  completeness,       -- điểm do code chấm, xem mục dưới
  reviewed_by
)
```

Đây chính là artifact `Physical design_chức năng [...]` trong bộ template ở
`/home/lupca/Documents/agmx` — quy trình của bạn đã có khái niệm này, chỉ chưa
đưa vào hệ thống.

### Phần WBS còn giữ

```sql
pm_wbs_node (
  id, project_id, parent_id, code, path,
  node_type,          -- deliverable | work_package
  stage,              -- frame|decompose|specify|build|verify|integrate|done
  title, description, acceptance_criteria,
  estimate_rounds, actual_rounds,
  assignee_agent_id, task_id,
  progress_pct, sort_order, version, archived_at
)

pm_document (id, project_id, doc_type, title, content, structured jsonb,
             version, supersedes_id)
pm_template (doc_type PK, section_schema jsonb, sample_content)
```

WBS **hạ xuống vai trò phụ** — nó là khung nhìn "việc" trên cùng đồ thị, không
phải trung tâm. Nhưng giữ lại vì hai lý do phục vụ chất lượng:

- **Quy tắc 100%**: tổng phạm vi node con phải phủ 100% node cha. Chống **sót
  phạm vi** — kiểu "tưởng thằng kia làm phần migration rồi". Đây là cơ chế chất
  lượng thật, không phải thủ tục hành chính.
- **Trả lời "có ai đang làm cái tương tự chưa"** trước khi tạo task mới.

`stage` là **cột trên node, không phải bảng cha**: agent chạy pipeline song song
nên "cả dự án đang ở giai đoạn thiết kế" không còn đúng. WP-1 có thể đang
`verify` trong khi WP-7 còn ở `specify`.

Hai điều chỉnh so với quy trình cũ: **gộp Coding + Unit test** (agent viết cả hai
cùng lúc, tách trên giấy chỉ làm sai số liệu) và **thêm `integrate`** (nợ tích hợp
do chạy song song sinh ra; tầng DEV đã có `land_task`).

## Cơ chế mất hiệu lực

Đây là thứ làm hệ này "sống".

```
commit mới
   ↓
diff ra danh sách symbol bị đụng
   ↓
tra spec_anchor: symbol trùng VÀ anchor_sha đã khác
   ↓
spec_item.status = 'stale'  + ghi lý do (symbol nào, commit nào)
   ↓
agent chỉ suy lại NHỮNG CÁI bị gắn cờ, phần còn lại truy vấn thẳng
```

Chạy trong worker, kích hoạt bởi cùng sự kiện commit mà `land_task` dùng. Không
cần LLM — thuần code.

**Đã triển khai (CTV2-1342):** `app.services.spec_anchor.apply_commit_staleness`,
gọi từ `_publish_graph_rebuild` trong `app.services.outbox` — cùng
`OutboxEvent(event_type="graph_rebuild_requested")` mà CTV2-1339 dùng để
rebuild graph, không dựng event mới. Với `.py`, `anchor_sha` băm bằng
`hash_symbol_source` trên khai báo cục bộ trích bằng AST. Với mọi file khác,
`anchor_sha` băm toàn bộ file; `symbol` chỉ là nhãn mô tả. Diff dùng
`git diff --name-only <before> <after>`: với sha đơn (merge commit của
`land_task`) thì `before = sha^1`; với `result_ref` dạng `<base>..<head>`
(worktree trước khi land) thì dùng thẳng làm range. Neo tạo qua op `"anchor"`
của tool `spec_write` sẵn có (không tách tool `spec_link` riêng — ngoài phạm
vi CTV2-1342). Đọc: tool `spec_stale(project)`, `required_role="executor"`.

Hệ quả quan trọng: **truy vấn spec trở nên rẻ và ổn định**. Hai agent hỏi cùng câu
ở cùng thời điểm sẽ nhận cùng câu trả lời, khác hẳn tình trạng "mỗi lần đọc ra
một kết luận".

## Trục THỰC HOÁ

`status` (draft/active/stale/superseded) chỉ nói về vòng đời của MỆNH ĐỀ —
đã viết chưa, còn đúng không, bị thay chưa. Không giá trị nào trong đó nói
được "đã thành code chưa". Bằng chứng nó có hại thật: 2026-08-05 điều phối
viết nhiều spec_item mô tả cơ chế (brief, unknowns, available_actions, và
chính trục này) mà không cái nào có một dòng code — không có gì trong
`status` từng khai báo điều đó.

**`spec_item.realization`** (CTV2-1395) là trục thứ hai, độc lập với
`status`: `agreed` (đã chốt mệnh đề) hoặc `built` (đã thành code). Một item
có thể `active` (đúng, đã chốt) mà vẫn `agreed` (chưa code) — gộp vào
`status` sẽ phá `stale`, vốn chỉ có nghĩa với code ĐÃ TỒN TẠI.

Đúng kỷ luật đã áp cho `stale_reason`: **`realization` chỉ được DẪN XUẤT,
không bao giờ ghi tay.** `spec_write` từ chối thẳng bất kỳ op nào mang
`realization` — top-level hay lồng trong `item`/`patch`/`new_item` của
create/update/supersede — không phân biệt nguồn (LLM hay tool caller). Cột
DB luôn mặc định `agreed` và không có đường ghi nào khác; giá trị THẬT được
`spec_get` tính lại từ đầu ở mỗi lần đọc (`_realization_projection` trong
`spec_service.py`), không đọc từ cột.

Ba điều kiện, cả ba máy kiểm được, ĐỦ để thành `built`:

```
1. có ít nhất một spec_anchor với relation='implements'
2. anchor đó GIẢI ĐƯỢC trong repo CHÍNH hiện tại (anchor_resolves trong
   spec_anchor.py -- luôn đọc working tree đã checkout, KHÔNG phải worktree
   executor đang ngồi)
3. có ít nhất một spec_task_link relation='implements' mà task đó
   status='done'
```

Thiếu điều kiện nào thì dừng kiểm ngay ở đó và trả `agreed` — không kiểm tiếp
các điều kiện sau, để `why` không bao giờ báo sai lý do (ví dụ báo "task chưa
done" trong khi thực ra chưa có anchor nào). Theo spec_item 78397775: mọi kết
quả dẫn xuất phải mang `why` + `next` NGAY TRONG CÙNG PAYLOAD:

```json
"realization": {
  "state": "agreed",
  "why": "chưa có anchor relation='implements' nào",
  "next": "land code rồi neo bằng spec_write (op='anchor', relation='implements')"
}
```

**Truy vấn backlog** (điều phối tự chọn việc không cần người nói):

```
backlog = spec_item active + chưa built
xong    = không còn item nào như vậy
```

Qua `spec_get`, hai pseudo-field trong `filter` (không phải cột SQL, lọc sau
khi dẫn xuất): `filter.backlog: true` (active + chưa built, tương đương
truy vấn backlog ở trên) và `filter.realization: "agreed"|"built"` (lọc trực
tiếp theo trạng thái đã tính). Mỗi item trả về từ `spec_get`/`spec_write`
luôn kèm khối `realization` — không cần filter riêng mới thấy được.

**Đo trên dữ liệu thật (2026-08-05, `_realization_projection` chạy qua
`spec_get` trên DB thật, không phải giả lập):** 14 spec_item project
`agenticmatix` được tạo ngày đó → **13 `agreed`, 1 `built`**. Item `built`
duy nhất là "Hợp đồng mô tả tool" (neo `describe_problems` trong
`tool_argument_validator.py`, liên kết task CTV2-1392 đã `done`). 13 item
còn lại đều `agreed` — không phải vì thiếu anchor (một số đã có anchor
`implements` giải được) mà vì **task `implements` liên kết vẫn `todo`**,
đúng điều kiện 3 chưa thoả. Số liệu này KHÁC giả định ban đầu lúc viết task
CTV2-1395 (ước lượng "4 agreed" tức ngầm định phần lớn đã `built`) — số thật
thấp hơn nhiều vì phần lớn task hiện thực hoá các spec_item đó (CTV2-1390,
1391, 1393, 1394, 1396, 1397) chưa `done`. Đây không phải lỗi cơ chế: đúng là
mục đích của trục này — lộ ra chính xác cái gì mới CHỐT chứ chưa THÀNH CODE,
kể cả khi con số đó khó nhìn hơn dự kiến.

## Phát hiện trùng và xung đột

Luồng khi có ý tưởng chức năng mới — thay thế việc "nhờ agent đọc spec":

```
mô tả chức năng mới
   ↓ embed
spec_search → top-K spec_item gần nghĩa  (pgvector)
   ↓
với mỗi ứng viên: kiểm tra neo có chồng lấn code không (dùng code graph)
   ↓
phân loại: duplicates | conflicts_with | refines | không liên quan
   ↓
ghi vào spec_relation → lần sau khỏi phán lại
```

Bước "ghi lại" là mấu chốt: kết luận trùng/xung đột được **lưu**, không phán lại
từ đầu mỗi lần.

> **Phụ thuộc:** embedding đã chạy (đo 2026-08-04: `agenticmatix` 1582 node,
> `voma-invoice` 1409 node). Nhưng embedding **mục ruỗng theo thời gian** — mỗi
> symbol mới là một node chưa embed — nên phải nằm trong chu trình build định
> kỳ, không phải chạy một lần.
>
> **Đồ thị cũ đi rất nhanh trong repo đang hoạt động:** `agenticmatix` build tại
> `8e67c07`, 25 phút sau HEAD đã là `e744d4a` (`head_matches_build: false`). Xem
> mục *Giữ đồ thị tươi*.

## Giữ đồ thị tươi

Đồ thị code là nền của cả `spec_search` lẫn cơ chế mất hiệu lực. Nó cũ thì cả hai
sai theo.

**Không dùng hook sau `land_task` làm cơ chế duy nhất.** Code thay đổi mà không
qua `land_task`: commit trực tiếp, push từ ngoài, người khác làm, đổi branch.
Bằng chứng: phiên 2026-08-04 tạo 5 commit, **không commit nào qua `land_task`**.

**Không rebuild đồng bộ khi gọi tool** — agent sẽ đứng hình.

Ba tầng:

```
1. Kiểm tra cũ/mới mỗi lần gọi tool        ← rẻ, KHÔNG chặn
   so head_sha vs built_at_sha (tool đã trả sẵn head_matches_build)
   → vẫn trả kết quả, kèm cảnh báo "đang cũ tại <sha>"

2. Rebuild INCREMENTAL theo sự kiện commit (không chỉ land_task)
   graph là incremental (có detect_changes_tool) → tính bằng giây
   → đẩy theo sự kiện, đừng để tool call kéo
   → embedding phải nằm trong đường này, không thì node mới không tra
     được theo ngữ nghĩa

3. Dùng projects.graph_status (cột đã có, cả 16 project đang 'idle')
   idle → stale → building → fresh
```

Nguyên tắc: **tươi là việc của sự kiện (push), lazy check chỉ là lưới an toàn và
tín hiệu trung thực** — không bao giờ để nó chặn.

### Đưa graph tool vào prompt executor

Hợp lệ về mặt cơ chế, đã xác nhận: group `research` chứa đúng hai tool,
`required_role="executor"` (`tool_registry.py:829,847`), và
`_research_repo_root` (`context_handlers.py:45`) lấy `repo_root` từ
**`Project.repo_root` trong DB** chứ không dò từ cwd — nên executor chạy trong
worktree vẫn trỏ đúng repo chính. `DEFERRED_GROUPS`/`load_tools` chỉ phục vụ
OpenAI tool loop; chúng không làm giảm danh sách schema của MCP.

Bốn cảnh báo:

- **Đường này chưa từng chạy thật.** `get_impact_radius`: **0 lần gọi**.
  `semantic_search`: 26 lần nhưng `task_id` NULL toàn bộ — chưa lần nào từ
  context task. Chạy thử một task thật trước khi nhét vào mọi prompt.
- Handler `get_impact_radius` nhận `args.strip()` thô
  (`context_handlers.py:92`) — prompt phải ghi rõ định dạng đầu vào, không thì
  agent đoán rồi gọi hỏng.
- Kết quả phản ánh **repo chính, không phải worktree** agent đang sửa. Đúng cho
  "hiểu code trước khi làm", sai nếu dùng để kiểm tra thay đổi của chính mình —
  phải nói rõ trong prompt.
- Chèn text vào **mọi** prompt tốn token mọi lượt, trong khi 65% task one-shot
  có thể không cần. Viết cực ngắn hoặc chèn có điều kiện.

## Bản thiết kế thực thi: chốt trước khi dispatch

### Vì sao

Với mô tả 250 ký tự, model **vẫn phải thiết kế** — nhưng nó thiết kế **bên trong
mỗi lượt build**, và mỗi lần retry lại thiết kế lại từ đầu, mỗi lần một khác.
Chính là bệnh tái suy diễn ở cấp task.

Tách ra thì được **chênh lệch giá**:

```
HIỆN TẠI:  mô tả mỏng → [model mạnh: thiết kế + code] × 1.82 vòng
ĐỀ XUẤT:   mô tả mỏng → [model mạnh: thiết kế] × 1 → [model rẻ: code] × N vòng
```

Thiết kế một lần bằng model mạnh, thực thi nhiều lần bằng model rẻ. Retry chỉ
lặp lại phần rẻ. Đây cũng là câu trả lời thực tế cho mục tiêu giảm chi phí — theo
**quota**, thứ đo được, chứ không theo đô-la, thứ hiện không đo được.

### Nội dung bắt buộc

Đủ chi tiết để một LLM yếu nhìn vào là làm được, không phải tự quyết định gì:

- **`files`** — đường dẫn cụ thể, tạo/sửa/xoá, và *vì sao* file đó
- **`changes`** — từng symbol: chữ ký hàm, hành vi mong đợi, các ca biên
- **`data_changes`** — migration, đổi schema, tương thích ngược
- **`test_plan`** — test nào phải thêm/sửa, khẳng định điều gì
- **`risks`** — chỗ nào dễ hỏng, cần chú ý

### Chốt chặn

```
task chỉ dispatch được cho executor rẻ khi:
  impl_design tồn tại  VÀ  completeness >= ngưỡng
ngược lại → hoặc dispatch cho model mạnh, hoặc chặn và đòi thiết kế trước
```

`completeness` do **code chấm**, không phải LLM tự chấm: có ít nhất 1 file, mỗi
file có lý do, mỗi thay đổi có symbol + hành vi, có test_plan, mọi symbol nêu ra
đều tồn tại trong code graph (hoặc được đánh dấu là tạo mới). Kiểm tra cơ học,
không cần suy luận.

## Ranh giới code / LLM

Nguyên tắc: **LLM soạn và phán đoán; code kiểm tra, tính toán, và ghi nhớ.**

| Việc | Ai làm |
|---|---|
| Gắn cờ spec lỗi thời khi commit | Code |
| Dẫn xuất `realization` (agreed/built) của spec_item | Code |
| Chấm `completeness` của impl_design | Code |
| Kiểm tra quy tắc 100% của WBS | Code |
| Sinh mã WBS `1.2.3`, rollup tiến độ | Code / SQL |
| Đối chiếu symbol trong design với code graph | Code |
| Gợi ý `estimate_rounds` từ node tương tự | SQL |
| — | — |
| Soạn `spec_item`, ADR, charter, scope | LLM |
| Soạn `impl_design` | LLM (model mạnh) |
| Phán "trùng hay xung đột" trên ứng viên đã lọc | LLM |
| Phân rã scope → WBS | LLM |

Điểm mấu chốt: LLM **không bao giờ** được hỏi "cái này còn đúng không" — đó là
việc của cơ chế mất hiệu lực. Hỏi LLM tức là quay lại tái suy diễn.

## Tool surface

**Một MCP duy nhất, KHÔNG tách endpoint.** `DEFERRED_GROUPS` và `load_tools`
không áp dụng cho MCP: chúng là cơ chế riêng của OpenAI tool loop, còn MCP
luôn công bố projection của registry khi client gọi `tools/list`. Vì vậy không
được dùng việc thêm group hoặc gọi `load_tools` để tuyên bố đã cô lập context
MCP.

MCP lọc projection theo role của token ngay lúc liệt kê: coordinator thấy toàn
bộ 28 tool hiện có, executor chỉ thấy 7 tool có
`required_role="executor"` (khoảng 79% schema context được loại bỏ). FastMCP
gọi `list_tools()` theo từng request, nên server dùng một endpoint duy nhất và
đọc role từ header của từng kết nối; không cần hai instance hoặc hai đường dẫn.
Kiểm tra `required_role` trong handler vẫn giữ nguyên và là ranh giới an ninh,
còn lọc lúc liệt kê chỉ là tối ưu context.

> Bản `08-pm-layer.md` từng đề xuất tách `/mcp/pm` và `/mcp/dev`. **Sai.** Lý do
> bác bỏ: **executor cũng là người tiêu thụ spec chính** — nó phải tra spec và
> code graph *trước khi* code. Tách endpoint thì executor không với tới spec nếu
> không nối hai kết nối, hai token. Giả định sai ban đầu là "PM và DEV là hai
> nhóm người dùng tách biệt".
>
> Do đó `spec_*` phải để `required_role="executor"`, giống
> `get_minimal_context`/`get_impact_radius` hiện nay.

```
spec_search(project, query, kinds?)     → tìm trùng/xung đột TRƯỚC khi tạo task           [chưa làm]
spec_get(ids[] | task_id | filter)      → đọc item + relation + anchor + task link         [đã làm]
spec_write(ops[])                       → tạo/sửa/supersede + neo code/task theo lô         [đã làm]
spec_stale(project)                     → cái gì đang lỗi thời và vì sao                   [đã làm]
impl_design(action, task_id, ...)       → soạn/đọc/chấm bản thiết kế thực thi              [đã làm]
pm_plan(project, ops?)                  → WBS: đọc cả cây hoặc sửa theo lô                 [chưa làm]
pm_document(action, project, doc_type)  → charter/scope/template                           [chưa làm]
```

Tối ưu token bằng **một lệnh đọc béo + mutation theo lô**: `spec_get` và
`pm_plan` trả nguyên cụm thay vì để LLM đi từng node. Tài liệu **không có gate**,
chỉ version + `supersedes_id`.

## Bàn giao sang tầng DEV

```
PM stage      DEV task status
specify   →   impl_design đạt ngưỡng → create_task → todo
build     →   dispatched / running          (executor rẻ)
verify    →   awaiting-review / in-review
integrate →   land_task → chạy cơ chế mất hiệu lực
done      →   done
              changes-requested → quay lại build; ghi nhận vào actual_rounds
```

**Cardinality 1:1.** Rework dùng lại chính task đó (`task_rounds`), nên
`pm_wbs_node.task_id` đơn trị là đủ. Cần N task thì phân rã node sâu thêm — cũng
là điều quy tắc 100% đòi hỏi.

**Tầng DEV gần như không phải đổi gì:**

- `task_events` + outbox đã có → đồng bộ `stage` tự động
- `land_task` đã có → móc thêm bước gắn cờ spec lỗi thời
- `depends_on` giữ nguyên
- **Thay đổi DEV duy nhất**: thêm chốt `impl_design` vào điều kiện dispatch

## Đã cắt gì khỏi bản trước và vì sao

Bản `08-pm-layer.md` có những phần sau, nay **bỏ** vì không phục vụ hai mối lo đã
nêu (chất lượng, lệch spec):

| Phần bị cắt | Lý do |
|---|---|
| CPM / critical path | Công cụ dự báo *ngày*. Nền móng lập lịch cũng đang hỏng: concurrency là bể chung không lọc project (`task_validators.py:191`), và **không có hàng đợi** — `check_brakes` trả `queue=True, retry_after_seconds=30` tức là *retry*; `tasks.priority` chỉ dùng chọn agent (`agent_matcher.py:234`), không xếp thứ tự dispatch |
| `pm_baseline` / variance | Để báo cáo cho khách hàng bên ngoài. Bối cảnh hiện tại không có |
| `pm_report` / Gantt | Trang trí |
| `pm_portfolio_policy` | Không liên quan chất lượng |
| `estimate_cost_usd` | Không đo được (xem cảnh báo chi phí) |
| Milestone | Đã cân nhắc và bỏ từ bản trước — không có mốc hợp đồng |

**Vẫn nên làm nhưng tách riêng, không thuộc tầng này:** sửa hàng đợi dispatch từ
retry ngẫu nhiên thành hàng đợi có thứ tự. Đây là lỗi vận hành thật (23 lần
`dispatch_queue` bị từ chối), đáng sửa độc lập.

## Thứ tự triển khai

Xếp sao cho thứ rẻ và chặn cửa đi trước:

```
0. Nền đo lường
   0.1  Sửa attribution llm_usage (điền task_id + agent_run_id)
   0.2  Giữ đồ thị tươi: lazy staleness check + rebuild incremental theo
        sự kiện commit + nối projects.graph_status  (embed đã xong lần đầu)
   0.3  Chạy thử graph tool từ MỘT task thật (đường executor chưa từng chạy)
        rồi mới thêm hướng dẫn vào prompt

1. Lõi spec sống                     2. Chất lượng đầu vào
   1.1  Bảng spec_* + migration [xong]  2.1  impl_design + chấm completeness
   1.2  spec_write / spec_get [xong]    2.2  Chốt dispatch theo completeness
   1.3  Neo + cơ chế mất hiệu lực       2.3  Thí nghiệm đối chứng: đo vòng
        [xong — CTV2-1342]                  trước/sau khi có impl_design
   1.4  spec_search (cần 0.2)
   1.5  Trục THỰC HOÁ [xong — CTV2-1395]

3. Khung nhìn việc
   3.1  pm_wbs_node + quy tắc 100%
   3.2  pm_document + pm_template (nạp từ Documents/agmx)
   3.3  Đồng bộ stage từ task_events
```

Mục **0.2 chặn 1.4**: không embed thì không tìm được trùng theo ngữ nghĩa.
Mục **2.3** là phép thử thật cho giả thuyết "đặc tả kỹ thì ít làm lại" — nếu số
liệu không ủng hộ thì phải xem lại phần 2, đừng xây tiếp.

## Việc còn treo

- Nạp `/home/lupca/Documents/agmx` vào `pm_template`: phải tách **cấu trúc rỗng**
  (thành template) khỏi **nội dung NextEvent** (là dữ liệu mẫu). File hiện lẫn cả
  hai.
- Kiến thức nền tảng quản lý dự án (vì sao cần WBS, khi nào dùng): dùng
  `knowledge_items` sẵn có, ngoài phạm vi bản này.
- Ngưỡng `completeness` bao nhiêu là đủ: phải hiệu chỉnh bằng dữ liệu thật ở mục
  2.3, đừng chọn số tuỳ ý.
