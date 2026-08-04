# AGENT PLAYBOOK — cách làm việc trong dự án này

> Dành cho AI agent (Claude/bất kỳ) vào phiên mới với repo agenticmatix.
> File này chỉ giữ thứ cần để **bắt đầu làm việc**. Bằng chứng, số liệu và
> nhật ký sự cố nằm trong `knowledge_items` — tra bằng `manage_knowledge` hoặc
> `query_db` khi cần chiều sâu (xem mục 6).
>
> Đọc file này TRƯỚC, rồi `docs/spec/01..08` khi cần chi tiết.

## 0. Người bạn đang làm việc cùng

lupca — tiếng Việt, vai quản lý (không đọc sâu code, vì thế mới xây hệ này).

- **Sửa luôn, đừng hỏi** với việc thuận chiều; chỉ hỏi khi quyết định thiết kế
  thật sự thuộc về user (và khi hỏi: đưa phương án + khuyến nghị).
- **Ghét im lặng**: gate/câu hỏi phải thành CÂU HỎI ở cuối câu trả lời, không
  chôn trong báo cáo. Hệ đã có máy nhắc `pending_approvals` — đừng phá nó.
- **Quyền tự duyệt gate**: cứ `approve_gate` thẳng cho `dispatch` và
  `review_order`. VẪN hỏi khi: **verdict** (đọc kết luận reviewer trước, đừng
  duyệt mù), **admin gate**, và **việc khó đảo ngược** (xoá dữ liệu, viết lại
  lịch sử git, đụng code chưa commit của user).
- **DB là source of truth duy nhất**: mọi bug/feature/quyết định ghi thành task
  CTV2-xxx. Xong việc → đánh done trong DB.
- **KHÔNG dùng fable**. Commit + push thẳng `main`, message nói VÌ SAO.
- **Báo cáo trung thực nguyên văn**: failed là failed; khoe cả cái mình làm hỏng.
- Chỉ làm dự án đang được giao — task của project khác thì để nguyên.
- **Bảo vệ repo chính và backend**: Executor CHỈ được thao tác git trong worktree riêng (`/tmp/control-tower-worktrees/...`). KHÔNG checkout/reset/stash trong repo chính, KHÔNG gửi SIGTERM/restart backend giữa chừng làm sập session coordinator.

## 1. Vòng lặp giải quyết vấn đề (làm đúng thứ tự này)

```
0. ĐO TRƯỚC KHI KHẲNG ĐỊNH — chạy một truy vấn/lệnh chứng minh, đừng suy từ
   tên hàm, tài liệu, hay mô hình kinh doanh. Và đo xong còn phải biết con số
   đó MANG Ý NGHĨA GÌ (cùng đơn vị? cùng phạm vi?).   [chi tiết: knowledge]
1. TÁI HIỆN trước, tin transcript sau — chạy lệnh/gọi tool y hệt kịch bản lỗi.
2. TRUY GỐC trong code — grep đến tận dòng, đọc cả caller lẫn callee.
   Hỏi "vì sao nó THIẾT KẾ như vậy" trước khi đổi.
3. SỬA NHỎ VÀ THẬT — fix tối thiểu đúng gốc. Không nới validation để "cho qua".
4. TEST toàn suite + viết test khoá hành vi mới.
5. COMMIT + PUSH ngay từng fix.
6. RESTART đúng cách (mục 3) — đổi code service dùng bởi worker thì restart CẢ worker.
7. VERIFY SỐNG qua MCP tool surface — không tin "chắc là được".
8. GHI SỔ: task DB + cập nhật `docs/spec` cùng commit.
```

Triage: bug CHẶN pipeline → sửa tại trận. Có đường vòng → ghi task DB ưu tiên
rõ, và NÓI CHO USER BIẾT nó đang nằm sổ.

## 2. Bộ đồ nghề

```bash
# DB thật (production!):
docker exec -i agmx_db psql -U ct -d control_tower   # port 5433, user ct

# Verify code executor TRƯỚC khi duyệt verdict (reviewer thường không chạy full suite):
git worktree add /tmp/wt-X <head-commit>
cd /tmp/wt-X/backend && <repo>/backend/venv/bin/python -m pytest tests -q

# Ngân sách context tool cho executor (phải ~7, không phải 30):
backend/venv/bin/python -c "
from app.services.tool_registry import get_mcp_tool_specs
s=get_mcp_tool_specs()
print('executor:', sum(1 for x in s if x.required_role=='executor'), '/', len(s))"

# Model agy hợp lệ (in danh sách khi truyền sai) — KHÔNG có bậc 'lite', là 'flash'
agy --model xxx --print x 2>&1 | head -15
```

Bẫy shell: `pkill -f "pattern"` tự bắn trúng shell của chính lệnh → dùng bracket
`pkill -f "app.mcp_nativ[e]"`. `cd backend` trong compound command hay lạc cwd →
luôn dùng đường dẫn tuyệt đối.

Bộ truy vấn đo sức khoẻ (số vòng, thời lượng run, tool_metrics): xem knowledge
*"Số liệu nền hệ thống agentic"*.

## 3. Vận hành (đúng một cách duy nhất)

```bash
./scripts/start-backend.sh    # docker db+redis → alembic head → mcp :8100 → worker
```

- **Restart**: `./scripts/stop-backend.sh`, đợi, rồi start lại.
- **SAU restart phải kiểm bằng LOG CÓ TRAFFIC THẬT + `ps` đúng PID mới.**
  `curl /health` trả `ok` KHÔNG chứng minh gì — server **cũ** có thể vẫn giữ
  cổng và trả lời, bản mới chết im lặng với `address already in use`. Đã dính,
  hậu quả là worker nhân bản 4 → 8.
- **Đừng restart giữa lúc có run đang chạy** — worker spawn CLI thật, giết giữa
  chừng làm run chết (`reaped: worker process is dead`). Kiểm trước:
  `SELECT count(*) FROM agent_runs WHERE status IN ('running','queued')`.
- KHÔNG `docker compose up` cả cụm: mcp/worker phải chạy local vì chúng spawn
  CLI thật và đụng repo thật (worktree, landing merge).
- `migrate_md_to_db.py` có guard `--yes-clear` — chạy lại là PHÁ state sống.

## 4. Điều phối task qua hệ thống (dogfood — cách ship feature chuẩn)

```
create_task {title, project}                      # id tự sinh; CHỈ 3 field
update_task {task_id, patch:{raw_input, plan, acceptance_criteria, priority, tags}}
dispatch_task {task_id, executor}   # supervised → gate → approve_gate
wait_for_task {task_id, timeout_seconds}   # long-poll; ĐỪNG poll bằng query_db
# executor xong → driver TỰ tạo gate review_order
# muốn đổi reviewer: request_review {task_id, reviewer} rồi approve gate
# verdict gate: TỰ VERIFY trước khi approve (worktree + full suite, mục 2)
#   verdict fail hợp lệ thì approve cái fail đó → changes-requested →
#   cập nhật plan vòng mới → dispatch lại
# verdict pass → HỆ TỰ MERGE vào main → done + landed_ref
#   conflict/landing hỏng → sửa xong gọi land_task {task_id}
approve_gate {gate_record_id | task_id, decision: approved|rejected}
```

**Nội dung `raw_input` — thứ làm nên chất lượng:**
- **Số đo và bằng chứng**, không phải cảm tính ("158 bản ghi, cột X NULL 100%"
  thay vì "attribution có vẻ chưa đúng")
- **Tham chiếu `file.py:dòng`** cho từng chỗ cần đụng
- **Cái đã xác minh rồi** — để agent khỏi mò lại
- **"KHÔNG làm trong task này: ..."** — chặn phình phạm vi, và chặn thêm migration
- **Bẫy đã biết** kèm gợi ý hướng

> **Đừng biến độ dài thành chỉ tiêu.** Đo trong cùng một ngày: nhóm cần 2+ vòng
> có mô tả **dài gấp đôi**, nhiều `file:dòng` hơn, nhiều AC hơn — vì viết dài
> hơn cho task khó hơn. Độ dài là *hệ quả của độ khó*, không phải *nguyên nhân
> của chất lượng*. Khi thiết kế cổng chất lượng: **chấm tính kiểm chứng được**
> (file có thật? symbol có trong code graph?), **đừng chấm khối lượng**.

**Reviewer:** `@gemini-3.1-pro-high` là lựa chọn tin cậy nhất hiện nay.
Nghi rubber-stamp (pass 4/4 quá nhanh, 0 findings cho diff to) → tự verify kỹ
hơn trước khi approve.

**Thu hồi task `failed` oan** (code executor đã commit, hỏng ở khâu review):
```
attach_result {task_id, commit:<head>, option:"request_review"}
request_review {task_id, reviewer:"@gemini-3.1-pro-high"}
```

**Khi dispatch một đợt SONG SONG — ba luật bắt buộc:**

1. **CHỈ MỘT task được thêm alembic migration.** Alembic là chuỗi tuyến tính;
   hai revision song song = hai head = hỏng `upgrade` = backend không khởi động.
   Ghi "KHÔNG thêm migration" vào mô tả **mọi task còn lại**.
2. **Sau MỖI lần land, kiểm tra tích hợp** — không tin verdict:
   ```bash
   alembic heads                            # phải đúng 1 dòng
   git status --short                       # phải sạch
   MAX_CONCURRENT_RUNS=2 pytest tests -q    # full suite
   ```
3. **LAND ≠ CÓ HIỆU LỰC.** `land_task` merge vào main nhưng KHÔNG restart
   service. `land_task` giờ trả `runtime_warning` kèm số commit đang chờ —
   đọc nó. Restart khi hàng đã rỗng.

Chi tiết 4 sự cố thật: xem knowledge *"Nợ tích hợp"*.

## 5. Họ bug đặc trưng (nghi NGAY khi thấy triệu chứng)

| Triệu chứng | Họ bug | Vết cũ |
|---|---|---|
| Tool "nuốt" tham số, rơi về default | Mapping tay JSON→args trong `execute_tool` vứt field không khai | CTV2-233, 237, 228 |
| Constraint DB nổ lúc chuyển trạng thái | `autoflush=False` + raw UPDATE — flush trước CAS; `emit_task_event` TỰ COMMIT, cấm gọi giữa apply | CTV2-214 |
| Run dài tự chết "no progress" | CLI im lặng (`claude -p`) vs watchdog | CTV2-232 |
| Task kẹt trạng thái lỡ cỡ | Đường cancel/fail chưa qua orchestration | CTV2-231 (MỞ) |
| "Đã sửa mà vẫn thế" | Server/worker cũ còn chạy — check `ps` start time | nhiều lần |
| `failed` = "Review result does not match its schema" | **Reviewer hỏng, KHÔNG phải code hỏng.** Commit executor vẫn nguyên | CTV2-1349 |
| `failed` = "completed without committed changes" | Có thể agent làm ĐÚNG: gặp blocker và từ chối tạo commit giả. **Đọc run output trước khi kết luận** | CTV2-1340 |
| Agent chết ngay, `Exit code: 1`, retry đủ 3 lần | Model không tồn tại; `manage_agent` nhận bừa mọi chuỗi | CTV2-1343 (MỞ) |
| Executor báo `task_scope_violation` | Tool không có tham số `task_id` → guard chặn nhầm | CTV2-1353 |
| Test pass máy này fail máy kia | Test ăn theo biến môi trường thay vì tự set | CTV2-1346 (MỞ) |
| Số hiển thị `$0` / rỗng mà trông như bình thường | **Thất bại im lặng** — họ bug nguy hiểm nhất của hệ này | CTV2-1350/1351/1352/1354 |

Nguyên tắc khi sửa hệ: thêm field vào ToolSpec schema thì PHẢI thêm vào mapping
+ test e2e tầng MCP (pattern trong `tests/test_mcp_native.py`).

## 6. Bản đồ chân lý

| Nguồn | Chứa gì |
|---|---|
| `docs/spec/01..07` | Đặc tả sống — PHẢI cập nhật cùng commit khi đổi hành vi |
| `docs/spec/08-living-spec.md` | **THIẾT KẾ, chưa triển khai** — hệ spec sống. Vấn đề nó giải: **tái suy diễn** (đọc lại nhiều lần ra kết luận khác nhau) |
| DB `tasks` | Sổ công việc chính thức |
| DB `knowledge_items` | **Bằng chứng, số liệu, nhật ký sự cố** — `manage_knowledge` hoặc `query_db` |
| `tool_metrics` | Telemetry graph/ocr/review — lộ đường code chưa ai chạy |
| Memory của Claude | Cheatsheet vận hành + preference user |

Knowledge nên đọc khi cần chiều sâu:
- *"Đo trước khi khẳng định"* — 5 lần tự sai trong một phiên và cách tránh
- *"Nợ tích hợp"* — 4 sự cố pass review rồi vỡ khi ghép
- *"Số liệu nền hệ thống agentic"* — mốc so sánh: số vòng, thời lượng, cost, context

## 7. Tinh thần

- **Mỗi lần verify sống là một lần săn bug.** Các bug đắt nhất đều lộ khi tự tay
  đi lại đúng kịch bản user.
- **Thất bại im lặng là kẻ thù số một của hệ này.** Hệ báo thành công trong khi
  không làm gì: task `done`, test xanh, không lỗi — mà hành vi thật không đổi.
  Thấy số `0`/rỗng/mặc định thì hỏi ngay: *thật sự bằng 0, hay không đo được?*
- **Tự bác bỏ chính mình khi số liệu nói ngược.** Mất mặt 30 giây, cứu hàng tuần
  xây nhầm hướng. Nói thẳng sai chỗ nào, đừng lặng lẽ sửa.
- **Rút lại khẳng định thiếu cơ sở là đúng, kể cả khi sau đó nó thành đúng.**
  Đúng nhờ may không phải đúng nhờ bằng chứng.
- **Đừng bán quá lời.** Không hứa "giảm cost" khi chưa đo được cost.
- **Cân nhắc giá trị thật của việc mình đề xuất.** Từng thiết kế cả tầng PM
  (WBS/CPM/baseline/Gantt) rồi tự cắt ~60% khi soi lại theo đúng mối lo của
  user. Cắt sớm rẻ hơn xây xong mới bỏ.
- **Sự cố giữa chừng = dữ liệu quý** — ghi thành task/knowledge để lần sau tra được.
- Khi hệ escalate về human — đó là hệ HOẠT ĐỘNG ĐÚNG, mình chính là human đó.
