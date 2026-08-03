# 08 — Tầng PM (quản lý dự án)

> **TRẠNG THÁI: THIẾT KẾ, CHƯA TRIỂN KHAI.** File này mô tả tầng dự định xây,
> không phải hiện trạng code. Khi bắt đầu code, cập nhật lại mục nào đã có thật.
> Mọi bảng/tool có tiền tố `pm_` đều chưa tồn tại.

Tầng PM quản lý phần **trên** task: khung dự án, phân rã phạm vi, ước lượng,
lịch. Tầng DEV hiện tại (`01`–`07`) quản lý phần **thực thi**: dispatch, review,
land. Hai tầng dùng chung một DB và một process, tách nhau bằng endpoint và
tool registry.

## Vì sao tách

Agent PM không cần 28 tool của tầng DEV, agent DEV không cần biết WBS. Nhồi
chung làm thừa context ở cả hai phía. Tách bằng **hai endpoint trên cùng
process**, không phải hai process:

```
/mcp/pm    → 8 tool  pm_*      token role=pm
/mcp/dev   → 28 tool (hiện có) token role=coordinator|executor
```

Chung DB, chung model, một lần deploy. Hai process chỉ nhân đôi việc vận hành mà
không được thêm gì — mục tiêu là cô lập context, không phải scale.

## Stage: thuộc tính của node, không phải cái hộp

Điểm khác căn bản so với waterfall. Trong waterfall cả dự án nằm ở "giai đoạn
thiết kế" rồi cả dự án chuyển sang "giai đoạn code". Với agent, các work package
chạy **pipeline song song**:

```
WP-1 đang VERIFY  │  WP-3 đang BUILD  │  WP-7 còn ở SPECIFY
─────────────────── cùng thời điểm ───────────────────
```

Nên `stage` là cột trên node, không phải bảng cha. "GIAI ĐOẠN THIẾT KẾ" trong
file Excel cũ trở thành một **view** (`WHERE stage='decompose'`), Gantt vẫn vẽ
y hệt nhưng dữ liệu đúng thực tế.

Không có bảng milestone — đã cân nhắc và bỏ (dự án nội bộ, không có mốc hợp đồng).

### Sáu stage

| Stage | Thay cho | Ghi chú |
|---|---|---|
| `frame` | Charter, Requirements | Người chủ đạo |
| `decompose` | Phần kiến trúc của Design | Định **đường nối** để agent chạy song song không đụng nhau |
| `specify` | Detailed design | AC + test case + context. **Chốt chất lượng thật sự** |
| `build` | Coding **+** Unit test | Gộp — agent viết code và test cùng lúc |
| `verify` | Code review + Integration + System test | Nút cổ chai |
| `integrate` | *(không có tương đương cũ)* | Nợ tích hợp do agent chạy song song sinh ra |
| `done` | | |

Hai thay đổi so với quy trình cũ, cả hai đều có lý do thực nghiệm:

- **Coding + Unit test gộp** — agent không tách hai việc này, tách trên giấy chỉ
  làm sai số liệu.
- **`integrate` là stage mới** — khi 10 agent chạy song song chúng sinh xung đột
  branch và migration chồng nhau mà một người làm tuần tự không gặp. Tầng DEV đã
  có `land_task`, chính là bước này.

## Ước lượng: đếm vòng, không đếm phút

Đo trên 57 task đã chạy (`agent_runs`, tháng 7–8/2026):

```
Số vòng execute/task            Thời lượng mỗi run
1 vòng  ██████████████  37      execute  median 6.1'  p90 10.2'
2 vòng  ███              8      review   median 5.0'  p90  9.4'
3 vòng  ███              7
5 vòng  █                1      trung bình 1.82 vòng/task
6 vòng  ██               3      65% one-shot, 35% phải làm lại
7 vòng  █                1
```

Thời lượng một run gần như **hằng số** (~6 phút); phương sai nằm ở **số vòng**
(1→7). Nên ước lượng bằng phút là ước lượng sai biến số. Biến chính là
`estimate_rounds`, và thứ dự báo nó tốt nhất là `spec_clarity` — không phải độ
khó kỹ thuật.

Vì đã có 57 task lịch sử, `estimate_rounds` **gợi ý được từ node tương tự đã
xong**, không cần LLM đoán.

## Ràng buộc danh mục (portfolio) — điểm quan trọng nhất

Hiện có **17 project active**. Ba sự thật trong code tầng DEV chi phối toàn bộ
thiết kế lịch:

1. **Concurrency là bể chung toàn hệ thống.** `check_brakes`
   (`task_validators.py:191`) đếm *mọi* run `queued|running` **không lọc
   project**; `max_concurrent_runs` đọc qua `_setting()` (`:161`) là **global**.
2. **`resolve_autonomy` (`:101`) chỉ override được 3 khoá** theo project:
   `autonomy`, `auto_max_risk`, `auto_max_rounds`. Không có concurrency, không
   có budget. Thực tế cả 17 project đều để `autonomy_policy = NULL`.
3. **Không có ngân sách theo project.** `max_cost_usd_per_task` là global và
   theo *task*.

4. **Không có hàng đợi dispatch.** Khi hết slot, `check_brakes` trả
   `queue=True, retry_after_seconds=30` — đó là **retry**, không phải queue.
   `tasks.priority` tồn tại nhưng chỉ dùng để **chọn agent**
   (`agent_matcher.py:234,267`), chưa từng dùng xếp thứ tự dispatch.

Hệ quả: **không thể tính critical path độc lập cho từng dự án.** Dự án A chiếm
hết bể thì lịch dự án B trượt vì lý do nằm ngoài dự án B. Và vì cơ chế là retry
chứ không phải queue có thứ tự, task nào giành được slot là **gần như ngẫu
nhiên** — không FCFS, không ưu tiên (gate `dispatch_queue` đã bị từ chối 23 lần).

Nên tầng PM phải là **bộ lập lịch danh mục**, không phải 17 bộ lập lịch rời rạc:

```sql
pm_portfolio_policy (
  project_id PK,
  concurrency_weight,    -- phần chia của bể chung
  monthly_budget_usd,
  priority               -- dùng để xếp hàng đợi dispatch
)
```

Đường găng khi đó chạy qua **cổng người duyệt** và **tranh chấp bể chung**, không
qua khối lượng công việc. Histogram nhân lực cổ điển → đường cong concurrency +
ngân sách.

> **Thay đổi DEV duy nhất mà thiết kế này đòi hỏi:** thay cơ chế retry bằng một
> **hàng đợi dispatch có thứ tự**, xếp theo `pm_portfolio_policy.priority` rồi
> `tasks.priority`. Không có nó, mọi lịch PM tính ra đều sai hệ thống vì thứ tự
> giành slot là ngẫu nhiên. Đây cũng là thứ đang gây ra 23 lần `dispatch_queue`
> bị từ chối.

## Data model

```sql
pm_wbs_node (
  id, project_id,
  parent_id,              -- đệ quy
  code,                   -- '1.2.3' — CODE sinh, không phải LLM
  path,                   -- materialized path, query subtree một lần
  node_type,              -- deliverable | work_package
  stage,                  -- frame|decompose|specify|build|verify|integrate|done
  title, description,     -- description = WBS Dictionary (PMBOK)
  acceptance_criteria,

  estimate_rounds,        -- ước lượng chính
  estimate_minutes,       -- suy ra: rounds × ~11' (6.1 exec + 5.0 review)
  estimate_cost_usd,
  actual_rounds, actual_minutes, actual_cost_usd,
  spec_clarity,           -- dùng lại khái niệm sẵn có của tầng DEV
  progress_pct,           -- leaf: nhập | branch: ROLLUP tự tính
  assignee_agent_id,      -- FK agents
  task_id,                -- FK tasks (1:1, xem mục Bàn giao)
  sort_order, version, archived_at
)

pm_dependency (predecessor_id, successor_id, dep_type, lag_minutes)
  -- dep_type: FS | SS | FF | SF

pm_baseline      (id, project_id, name, captured_at)
pm_baseline_item (baseline_id, node_id, estimate_rounds, planned_start, planned_end)
  -- đây là cột "[Old] Duration" trong Excel, làm đúng cách

pm_document (id, project_id, doc_type, title, content, structured jsonb,
             version, supersedes_id)
  -- doc_type: charter | scope | logical_design | physical_design | ...

pm_template (doc_type PK, section_schema jsonb, sample_content)
  -- nạp từ /home/lupca/Documents/agmx

pm_portfolio_policy (project_id PK, concurrency_weight, monthly_budget_usd, priority)
```

Quy tắc bắt buộc trong code: **quy tắc 100%** — tổng phạm vi các node con phải
phủ 100% phạm vi node cha. Đây là thứ làm WBS khác một cái danh sách việc.

`archived_at`: theo lệ chung (ArchivableMixin), mọi query mặt tiền phải lọc
`archived_at IS NULL`.

## Ranh giới code / LLM

Nguyên tắc: **LLM chỉ soạn và hỏi, tuyệt đối không tính.**

| Việc | Ai làm |
|---|---|
| Sinh mã WBS `1.2.3` | Code |
| Rollup progress (trọng số theo `estimate_rounds`) | SQL recursive CTE |
| Critical path (CPM forward/backward, slack) | Python |
| Suy ngày từ dependency + duration + bể chung | Python |
| Variance vs baseline | SQL |
| Gợi ý `estimate_rounds` từ node tương tự | SQL |
| — | — |
| Soạn Charter / Scope / design doc | LLM |
| Phân rã scope → WBS | LLM |
| Trả lời "cái gì đang chặn X" | LLM (đọc kết quả code tính) |

Nếu để LLM tự cộng `estimate` của 24 node con thì vừa tốn token vừa sai.

## Tool surface — 8 tool

Tối ưu token bằng **một lệnh đọc béo + mutation theo lô**:

```
pm_get_plan(project, depth?, subtree?)   → cả cây + rollup + cờ critical path, MỘT lần gọi
pm_edit_wbs(project, ops[])              → create/update/move/delete theo lô
pm_set_schedule(node_ids[], patch)       → estimate/assignee/progress/stage
pm_link_dependency(ops[])                → add/remove dep theo lô
pm_document(action, project, doc_type)   → CRUD tài liệu (không gate)
pm_baseline(action, project, name?)      → capture / compare
pm_promote_to_task(node_ids[])           → bàn giao sang tầng DEV
pm_report(project, kind)                 → gantt | critical_path | variance | health
```

`pm_get_plan` trả cả cây một lần thay vì để LLM đi từng node — đây là khoản tiết
kiệm token lớn nhất.

Tài liệu **không có gate**. Chỉ version + `supersedes_id`.

## Bàn giao sang tầng DEV

Ánh xạ stage:

```
PM stage      DEV task status
specify   →   (node sinh spec) → create_task → todo
build     →   dispatched / running
verify    →   awaiting-review / in-review
integrate →   land_task
done      →   done
              changes-requested → quay lại build, đồng thời hạ spec_clarity
                                  và hiệu chỉnh lại estimate_rounds
```

**Cardinality 1:1.** Làm lại (rework) dùng lại chính task đó (`task_rounds`), nên
`pm_wbs_node.task_id` để đơn trị là đủ. Nếu một work package cần N task thì phân
rã node sâu thêm — điều này cũng ép giữ quy tắc 100%.

**Dependency chỉ dịch được FS.** PM giữ đủ FS/SS/FF/SF + lag để *lập lịch*;
`tasks.depends_on` của DEV chỉ là FS không lag. Nên chỉ dep loại FS dịch sang
`depends_on` (để **chặn** dispatch); SS/FF/SF ở lại PM như gợi ý *lịch*, không
chặn. Không cần sửa DEV.

**Phản hồi tiến độ đã có sẵn.** Bảng `task_events` + outbox pattern đã tồn tại;
PM đăng ký đọc và cập nhật `stage`/`progress_pct`. Không cần sửa DEV.

**Tổng kết: tầng DEV không phải đổi gì**, ngoài một việc cần kiểm chứng là hàng
đợi dispatch có tôn trọng `tasks.priority` không (xem mục Portfolio).

## Vấn đề còn treo

- Nạp `/home/lupca/Documents/agmx` vào `pm_template`: cần tách **cấu trúc rỗng**
  (đưa vào template) khỏi **nội dung NextEvent** (là dữ liệu mẫu, không phải
  template). File hiện đang lẫn cả hai.
- Kiến thức nền tảng quản lý dự án (vì sao cần WBS, khi nào dùng) — để sau, dùng
  `knowledge_items` sẵn có, ngoài phạm vi bản này.
- Vector search chỉ hợp cho "tìm dự án tương tự đã làm", **không** hợp cho sinh
  tài liệu theo template (cần cấu trúc chính xác, không phải khớp ngữ nghĩa mờ).
