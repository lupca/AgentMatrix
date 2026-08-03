# AGENT PLAYBOOK — cách làm việc trong dự án này

> Dành cho AI agent (Claude/bất kỳ) vào phiên mới với repo agenticmatix.
> Chưng cất từ làm việc thật với lupca:
> - 2026-07-31 → 08-01: ~30 bug tìm-và-sửa, 6 feature ship qua hệ thống, 554 test xanh.
> - 2026-08-04: phiên thiết kế tầng spec sống + 7 task ship song song, 601 test xanh.
>   Bài học lớn nhất phiên này nằm ở mục 1.0 (đo trước khi khẳng định) và mục 5
>   (nợ tích hợp) — đọc kỹ hai mục đó.
>
> Đọc file này TRƯỚC, rồi `docs/spec/01..08` khi cần chi tiết.

## 0. Người bạn đang làm việc cùng

lupca — tiếng Việt, vai quản lý (không đọc sâu code, vì thế mới xây hệ này).
Kỳ vọng đã nói rõ nhiều lần:
- **Sửa luôn, đừng hỏi** với việc thuận chiều; chỉ hỏi khi quyết định thiết kế
  thật sự thuộc về user (và khi hỏi: đưa phương án + khuyến nghị).
- **Ghét im lặng**: gate/câu hỏi phải thành CÂU HỎI ở cuối câu trả lời, không
  chôn trong báo cáo. Hệ đã có máy nhắc `pending_approvals` — đừng phá nó.
- **Tiết kiệm**: KHÔNG dùng fable (Agent tool lẫn executor `@claude-fable`).
- **Quyền tự duyệt gate** (trao 2026-08-04): cứ `approve_gate` thẳng cho gate
  `dispatch` và `review_order` của việc đang làm dở, đừng chặn lại hỏi. VẪN hỏi
  khi: **verdict** (đọc kết luận reviewer trước, đừng duyệt mù), **admin gate**
  đổi cấu hình, và **việc khó đảo ngược** (xoá dữ liệu, viết lại lịch sử git,
  đụng code chưa commit của user). Vẫn tóm tắt cái đã duyệt trong câu trả lời.
- **DB là source of truth duy nhất**: mọi bug/feature/quyết định ghi thành
  task CTV2-xxx trong DB (repo md `~/projects/control-tower` đã bỏ, chỉ là
  snapshot legacy). Xong việc → đánh done trong DB.
- Commit + push thẳng `main`, gh account `lupca`, message nói VÌ SAO.
- Báo cáo trung thực nguyên văn: failed là failed; khoe cả cái mình làm hỏng.

## 1.0. ĐO TRƯỚC KHI KHẲNG ĐỊNH (bài học đắt nhất)

Trong một phiên duy nhất tôi đã khẳng định ba điều nghe rất hợp lý và **cả ba
đều sai**, mỗi lần chỉ cần một câu SQL hoặc một lệnh là lộ:

| Khẳng định | Thực tế đo được |
|---|---|
| "`spec_clarity` là biến dự báo số vòng tốt nhất" | Nhóm `high` **nhiều vòng hơn** (2.27 vs 1.72). Và 381/391 task chưa từng set nó |
| "`tasks.priority` chỉ cần kiểm chứng là xếp hàng đúng" | **Không có hàng đợi nào cả** — `check_brakes` trả `queue=True, retry_after=30s` tức là *retry*; priority chỉ dùng chọn agent |
| "Thêm group vào `DEFERRED_GROUPS` để tiết kiệm context MCP" | `get_mcp_tool_specs()` phơi **toàn bộ** tool qua MCP; docstring nói thẳng cơ chế deferred **không áp dụng** cho MCP |
| "Agent CLI chạy subscription nên không báo token/cost" | `--output-format json` cho ra **đầy đủ** token, và claude còn cho luôn `total_cost_usd`. Đo thật: `claude -p "say ok" --output-format json` → `cost_usd: 0.1366` |

Nguyên nhân chung: **suy từ tài liệu/tên gọi/mô hình kinh doanh thay vì đo**.
Tên hàm nghe như đang làm việc X không có nghĩa nó làm việc X; "subscription"
không có nghĩa là không có số liệu token.

Cái thứ tư đau nhất: tôi phát hiện nó **sau khi** đã commit chính mục này với ba
ví dụ đầu. Viết ra bài học không miễn nhiễm cho mình khỏi chính bài học đó —
phải *chạy lệnh*, mỗi lần.

Quy tắc: trước khi nói "hệ thống làm Y", chạy đúng một truy vấn hoặc một lệnh
chứng minh nó. Rẻ hơn nhiều so với xây cả một tầng lên trên giả định sai.

Hệ quả cho tài liệu: khi viết spec, **ghi kèm số đo và ngày đo**, và nếu bằng
chứng yếu thì nói rõ là yếu. Trong `docs/spec/08-living-spec.md` có hai khối
"CẢNH BÁO" kiểu đó — giữ nguyên, đừng lược đi cho gọn.

## 1. Vòng lặp giải quyết vấn đề (làm đúng thứ tự này)

```
1. TÁI HIỆN trước, tin transcript sau — chạy lệnh/gọi tool y hệt kịch bản lỗi.
   (Nhiều lần "lỗi" coordinator báo chỉ là thông tin cũ; server mới là chân lý.)
2. TRUY GỐC trong code — grep đến tận dòng, đọc cả caller lẫn callee.
   Đừng dừng ở triệu chứng: hỏi "vì sao nó THIẾT KẾ như vậy" trước khi đổi.
3. SỬA NHỎ VÀ THẬT — fix tối thiểu đúng gốc, kèm comment nêu ràng buộc
   (không comment kiểu nhật ký). Không nới validation/strict để "cho qua".
4. TEST: pytest toàn suite (backend/venv/bin/python -m pytest backend/tests -q)
   + viết test khóa hành vi mới. Có "test gap" (pass mà hành vi sai) thì viết
   test ĐO THẬT (thời gian block, row DB, file git...).
5. COMMIT + PUSH ngay từng fix (message giải thích vì sao, kèm bằng chứng live).
6. RESTART đúng cách (mục 3) — code service dùng bởi worker thì restart CẢ worker.
7. VERIFY SỐNG qua MCP tool surface — không tin "chắc là được".
8. GHI SỔ: task DB (done/todo) + cập nhật docs/spec cùng commit + GD4 plan.
```

Triage: bug CHẶN pipeline → sửa tại trận. Có đường vòng → ghi task DB ưu tiên
rõ, và NÓI CHO USER BIẾT nó đang nằm sổ (bài học: user hỏi "sao chưa sửa?").

## 2. Bộ đồ nghề riêng của dự án này

```bash
# Gọi MCP tool từ shell (coordinator thật sự dùng gì thì mình dùng nấy):
#   helper pattern "ct.py": fastmcp Client + StreamableHttpTransport
#   → http://localhost:8100/mcp, Bearer token đọc từ <coordinator-workspace>/.mcp.json
#   (workspace hiện tại: ~/^Coject-mangment/ — kiểm tra lại nếu đổi)
backend/venv/bin/python ct.py <tool> '<json-args>'

# DB thật (production!):
docker exec -i control_tower_db psql -U ct -d control_tower   # port 5433, user ct

# Đợi run xong (nền, tự đánh thức):
until [ "$(docker exec -i control_tower_db psql -U ct -d control_tower -tAc \
  "SELECT count(*) FROM agent_runs WHERE status IN ('running','queued')")" = "0" ];
  do sleep 30; done   # chạy run_in_background, kèm SELECT trạng thái cuối

# Verify code executor TRƯỚC khi duyệt verdict (reviewer thường không chạy full suite):
git worktree add /tmp/wt-X <head-commit>
cd /tmp/wt-X/backend && <repo>/backend/venv/bin/python -m pytest tests -q
```

Bẫy shell đã dính: `pkill -f "pattern"` tự bắn trúng shell của chính lệnh →
dùng bracket `pkill -f "app.mcp_nativ[e]"`. `cd backend` trong compound command
hay lạc cwd → luôn dùng đường dẫn tuyệt đối.

### Bộ truy vấn đo sức khoẻ (dùng `query_db`, dán thẳng)

```sql
-- Phân bố số vòng execute/task: biến số THẬT của chi phí, không phải thời lượng
WITH r AS (SELECT task_id, count(*) rounds FROM agent_runs
           WHERE kind='execute' GROUP BY task_id)
SELECT rounds, count(*) tasks FROM r GROUP BY rounds ORDER BY rounds;

-- Thời lượng run (2026-08-04: execute median 6.1', review 5.0' — gần như hằng số)
SELECT kind, count(*), round(percentile_cont(0.5) WITHIN GROUP
  (ORDER BY extract(epoch FROM (completed_at-started_at))/60)::numeric,1) median_min
FROM agent_runs WHERE status='success' AND completed_at IS NOT NULL GROUP BY kind;

-- Tool nào thật sự được gọi, có gắn task không (lộ đường code chưa ai chạy)
SELECT tool, source, count(*), count(task_id) co_task_id FROM tool_metrics
GROUP BY tool, source ORDER BY count(*) DESC;

-- Mô tả task đang mỏng cỡ nào (2026-08-04: trung bình 204–306 ký tự = 2–3 câu)
SELECT length(raw_input) FROM tasks WHERE raw_input IS NOT NULL;
```

```bash
# Ngân sách context tool cho executor (sau CTV2-1344 phải ~7 tool, không phải 30)
backend/venv/bin/python -c "
from app.services.tool_registry import get_mcp_tool_specs
s=get_mcp_tool_specs()
print('executor:', sum(1 for x in s if x.required_role=='executor'), '/', len(s))"

# Model agy hợp lệ (in ra danh sách khi truyền model sai) — KHÔNG có bậc 'lite'
agy --model xxx --print x 2>&1 | head -15
```

## 3. Vận hành (đúng một cách duy nhất)

```bash
./scripts/start-backend.sh    # docker db+redis → alembic head → mcp :8100 → worker
# restart: kill $(cat .backend.pid); pkill -f "venv/bin/dramati[q]"; rm -f *.pid; chạy lại script
# SAU restart LUÔN: curl -s localhost:8100/health (200) + ps xem đúng pid mới
#   (đã dính: server cũ giữ port, bản mới chết im lặng → tưởng đã restart)
```
- KHÔNG `docker compose up` cả cụm: mcp/worker phải chạy local vì chúng spawn
  CLI thật (claude/codex trên host) và đụng repo thật (worktree, landing merge).
- `migrate_md_to_db.py` có guard `--yes-clear` — chạy lại là PHÁ state sống.
- Test suite an toàn: sqlite in-memory + postgres `control_tower_test` riêng;
  `record_tool_metric` tự tắt khi TESTING=1.

## 4. Điều phối task qua hệ thống (dogfood — cách ship feature chuẩn)

```
create_task {title, project}                      # id tự sinh; CHỈ 3 field
update_task {task_id, patch:{plan, acceptance_criteria, priority, tags}}
  # plan = spec ĐẦY ĐỦ: bối cảnh + file đích + từng việc + ràng buộc + lệnh test
  # (spec kỹ như viết cho người mới — chất lượng output tỷ lệ thuận trực tiếp)
generate_spec_plan {task_id, agent_id:"@sonnet-spec-plan"}  # research-first:
  # agent CLI đọc repo thật; trả open_questions + spec_clarity; còn câu hỏi
  # → CHẶN dispatch; đổ câu trả lời user vào raw_input rồi regenerate
dispatch_task {task_id, agent_id}   # supervised → gate → approve_gate
  # agent_id ĐƯỢC tôn trọng (alias của executor); executor tốt: @claude-sonnet-low,
  # @gpt-5.6-sol; task read-only: tag "no-commit" → executor in 'RESULT_REF: none'
wait_for_task {task_id, timeout_seconds}  # long-poll; không truyền cursor = chỉ chuyện MỚI
# executor xong → driver TỰ tạo gate review_order (approve nó; muốn đổi reviewer:
#   reject gate đó rồi request_review {task_id, agent_id:"@gpt-5.6-luna-high"})
# verdict gate: TỰ VERIFY trước khi approve (worktree + full suite, mục 2);
#   verdict fail hợp lệ thì approve cái fail đó → changes-requested →
#   update_task plan vòng mới (nêu đích danh findings + 'merge <head cũ> trước')
#   → dispatch lại THẲNG (không cần SQL). auto_max_rounds=3 → escalate human replan.
# verdict pass → HỆ TỰ MERGE vào main (landing) → done + landed_ref; conflict
#   → escalation, sửa xong gọi land_task {task_id}.
approve_gate {task_id | "admin:<id>", decision: approved|REJECTED}  # reject dùng được!
```

Đừng gọi `request_review` khi driver đã tạo gate (gate trùng — CTV2-230).
Nghi rubber-stamp (pass 4/4 quá nhanh, 0 findings cho diff to)? → tự verify kỹ
hơn trước khi approve; đã từng phải reject verdict rởm của agy.

**Reviewer (số liệu 2026-08-04):** `@gemini-3.1-pro-high` review 4 task đều trả
kết quả hợp lệ — hiện là lựa chọn tin cậy nhất. **TRÁNH `@claude-opus` làm
reviewer** cho tới khi CTV2-1349 xong (2/2 lần trả sai schema). Muốn chỉ định
reviewer riêng: `request_review {task_id, reviewer}` rồi approve gate — nó ghi
đè đề xuất của matcher.

### Mô tả task: đây là đòn bẩy chất lượng lớn nhất

Đo 2026-08-04: mô tả task trung bình **204–306 ký tự** (2–3 câu). Nhóm mô tả
ngắn nhất có số vòng cao nhất (2.00 so với 1.50–1.62). Nhiều acceptance criteria
**không** làm tăng số vòng — mô tả mỏng mới làm.

Viết `raw_input` dài **1.500–2.500 ký tự**, gồm:
- **Số đo và bằng chứng**, không phải mô tả cảm tính ("158 bản ghi, cột X NULL
  100%" thay vì "attribution có vẻ chưa đúng")
- **Tham chiếu `file.py:dòng`** cho từng chỗ cần đụng
- **Cái đã xác minh rồi** — để agent khỏi mò lại
- **"KHÔNG làm trong task này: ..."** — chặn phình phạm vi, và chặn thêm migration
- **Vướng mắc kỹ thuật đã biết** kèm gợi ý hướng, để agent không đâm đầu vào tường

### Thu hồi task bị `failed` oan (đừng dispatch lại — phí công executor)

Nếu code executor đã commit mà task `failed` ở khâu review:

```
attach_result {task_id, commit:<head>, option:"request_review"}
request_review {task_id, reviewer:"@gemini-3.1-pro-high"}
approve_gate  {gate_record_id}
```

`attach_result` nhận cả hash đơn lẫn dải `a..b`, tự chuẩn hoá thành
`<base>..<head>` (CTV2-1337 — trước đó nó lưu hash trần khiến `request_review`
từ chối vĩnh viễn, task kẹt `awaiting-review` không lối ra).

## 4.5. NỢ TÍCH HỢP — review từng task KHÔNG bắt được (bài học 2026-08-04)

Chạy 4 task song song (`MAX_CONCURRENT_RUNS=10`). **Cả 4 đều pass review, rồi
vỡ khi ghép.** Không reviewer nào sai — mỗi người chỉ thấy diff của task mình,
không ai nhìn được trạng thái sau merge.

| Vỡ ở đâu | Vì sao | Cách bắt |
|---|---|---|
| **Hai alembic head** | 1339 và 1341 mỗi task thêm revision cùng trỏ vào `045` → `upgrade head` FAILED, DB kẹt branchpoint, `start-backend.sh` chết | `alembic heads` phải ra ĐÚNG MỘT dòng |
| **Test hard-code `== 28`** | 1344 phát triển trên worktree tách trước khi 1341 thêm 2 tool → main thành 30 | Suy số từ registry, đừng hard-code |
| **`git stash pop` dở dang** | 3 file còn dấu `<<<<<<<`, `command_builder.py` không parse được | `git status` + `ast.parse` sau khi land |
| **Review sai schema** | reviewer hỏng → task bị đánh `failed` DÙ CODE HOÀN CHỈNH | Xem mục 5 |

**Luật rút ra — áp dụng mỗi khi dispatch một đợt song song:**

1. **CHỈ MỘT task trong đợt được thêm alembic migration.** Alembic là chuỗi
   tuyến tính; hai revision song song = hai head = hỏng `upgrade`. Ghi thẳng
   "KHÔNG thêm migration" vào mô tả **mọi task còn lại**, và ghi task nào đang
   giữ migration + `down_revision` phải trỏ vào đâu.
   Lỗi tôi mắc: phát hiện ràng buộc này SAU khi đã dispatch 1339 và không quay
   lại bổ sung → chính nó gây ra hai head.
2. **Sau MỖI lần land, chạy kiểm tra tích hợp** — không tin verdict:
   ```bash
   alembic heads            # phải đúng 1 dòng
   git status --short       # phải sạch
   MAX_CONCURRENT_RUNS=2 pytest tests -q    # full suite, không phải test của riêng task
   ```
3. **Vá tay khi hai head đã lỡ xảy ra:**
   ```bash
   alembic merge -m "merge <taskA> va <taskB>" heads && alembic upgrade head
   ```
4. Task **CTV2-1347** đã thêm guard (test một-head + chặn lúc land). Nếu guard
   báo, đừng vòng qua nó — tạo merge revision cho đàng hoàng.

Đây chính là stage `integrate` mô tả trong `docs/spec/08-living-spec.md`: khi
nhiều agent chạy song song, chúng sinh ra loại nợ mà một người làm tuần tự
không bao giờ gặp. Bốn sự cố trên là bằng chứng thực nghiệm cho điều đó.

## 5. Họ bug đặc trưng của codebase này (nghi NGAY khi thấy triệu chứng)

| Triệu chứng | Họ bug | Vết cũ |
|---|---|---|
| Tool "nuốt" tham số, hành vi rơi về default | Mapping tay JSON→args trong `execute_tool` (command_router ~400) vứt field không khai | CTV2-233 (decision), 237 (patch), 228 (agent_id) |
| Constraint DB nổ khó hiểu lúc chuyển trạng thái | `autoflush=False` + raw UPDATE / deferred trigger — flush trước CAS; `emit_task_event` TỰ COMMIT, cấm gọi giữa apply | CTV2-214, flush-CAS, landing-event |
| Run dài tự chết "no progress" | CLI im lặng (`claude -p`) vs watchdog; setting 2400s là tạm | CTV2-232 |
| UniqueViolation seq/attempt khi retry | Retry hygiene chưa xong — run mới (id mới) là sạch | CTV2-219 (MỞ) |
| Task kẹt ở trạng thái lỡ cỡ | Đường cancel/fail nào đó chưa qua orchestration | CTV2-231 (MỞ) |
| "Đã sửa mà vẫn thế" | Server/worker cũ còn chạy — check ps start time | nhiều lần |
| Task `failed` = "Review result does not match its schema" | **Reviewer hỏng, KHÔNG phải code hỏng.** Code executor vẫn nguyên vẹn trong commit | CTV2-1345, 1342 — cả hai reviewer `@claude-opus`; `@gemini-3.1-pro-high` review 4 task đều sạch. CTV2-1349 (MỞ) |
| Task `failed` = "completed without committed changes" | Có thể là agent làm ĐÚNG: nó gặp blocker và từ chối tạo commit giả cho đạt AC. **Đọc run output trước khi kết luận** | CTV2-1340 — nhờ đó tìm ra 2 bug thật |
| Agent chết ngay, `Exit code: 1`, retry đủ 3 lần | Model không tồn tại. `manage_agent` nhận bừa mọi chuỗi model, chỉ lộ lúc dispatch | `gemini-3.6-lite-high` (agy không có bậc "lite", là **flash**). CTV2-1343 (MỞ) |
| Graph tool trả "graph may not be built" dù đã build | Phản hồi vượt buffer stdio (`Separator is not found, and chunk exceed the limit`) rồi bị nuốt thành thông báo sai | CTV2-1345 — `graph_client` không truyền `detail_level`, nhận mặc định `standard` |
| Test pass máy này, fail máy kia | Test ăn theo biến môi trường thay vì tự set | `MAX_CONCURRENT_RUNS` — CTV2-1346 (MỞ) |

Nguyên tắc khi sửa hệ: thêm field vào ToolSpec schema thì PHẢI thêm vào mapping
+ test e2e tầng MCP (pattern trong `tests/test_mcp_native.py`).

## 6. Bản đồ chân lý

- `docs/spec/01..07` — đặc tả sống, PHẢI cập nhật cùng commit khi đổi hành vi.
- `docs/spec/08-living-spec.md` — **THIẾT KẾ, chưa triển khai**: hệ spec sống
  (spec/ADR thành dữ liệu có neo vào code, tự gắn cờ khi lỗi thời). Vấn đề gốc
  nó giải: **tái suy diễn** — đọc lại nhiều lần cho ra kết luận khác nhau, vì
  đọc tức là lấy mẫu. Cách chữa: ghi lại cái đã suy ra kèm nguồn gốc và điều
  kiện hết hạn, lần sau truy vấn thay vì suy lại.
- `docs/plans/GD4-CLEANUP-PLAN.md` — backlog + nhật ký phát hiện.
- DB `tasks` (CTV2-2xx) — sổ công việc chính thức; `tool_metrics` — telemetry
  graph/ocr/review; `query_db` để phân tích.
- Memory của Claude (`~/.claude/projects/-home-lupca-projects-agenticmatix/memory/`)
  — cheatsheet vận hành + preference của user.

## 7. Tinh thần (thứ làm nên "mượt")

- Mỗi lần verify sống là một lần SĂN BUG: các bug đắt nhất (reject→done,
  patch no-op, wait trả sớm) đều lộ khi tự tay đi lại đúng kịch bản user.
- Sự cố giữa chừng = dữ liệu quý: đừng chỉ gỡ cho xong — ghi thành CTV2 record
  với triệu chứng/gốc/fix để lần sau ai gặp là tra được.
- Làm việc nền: chờ run bằng background until-loop, trong lúc đó làm việc khác
  (ghi sổ, viết spec task kế) — đừng ngồi nhìn.
- Khi hệ escalate về human ("round limit", "landing conflict") — đó là hệ HOẠT
  ĐỘNG ĐÚNG, mình chính là human đó: xử lý rồi ghi lại đường xử lý.
- Nói với user bằng kết quả trước, chi tiết sau; nhận sai thẳng thắn
  (commit sweep nhầm, guard tự bắn...) — lupca đánh giá cao điều đó.
- **Tự bác bỏ chính mình khi số liệu nói ngược.** Trong một phiên tôi đã lật ba
  khẳng định của chính mình (mục 1.0) và sửa lại doc đã chốt với user. Việc đó
  làm mất mặt trong 30 giây nhưng cứu được hàng tuần xây nhầm hướng. Khi lỡ đưa
  user một kết luận sai, **nói thẳng là tôi đã sai chỗ nào và vì sao**, đừng
  lặng lẽ sửa.
- **Đừng bán quá lời.** User hỏi "giảm cost vẫn ok chứ?" — câu trả lời trung
  thực lúc đó là "không, hiện KHÔNG đo được cost" (`_task_cost` luôn trả 0,
  brake chưa từng chạy, agent CLI chạy subscription nên không báo token). Sửa
  attribution trước, rồi mới nói tới giảm.
- **Cân nhắc giá trị thật của việc mình đang đề xuất.** Tôi từng thiết kế cả
  tầng PM (WBS, critical path, baseline, Gantt) rồi tự cắt ~60% khi soi lại
  theo đúng hai nỗi lo của user (chất lượng, lệch spec). CPM và Gantt là công
  cụ dự báo *ngày* — user không hỏi về ngày. Cắt sớm rẻ hơn xây xong mới bỏ.
