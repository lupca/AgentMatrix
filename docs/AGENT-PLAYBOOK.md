# AGENT PLAYBOOK — cách làm việc trong dự án này

> Dành cho AI agent (Claude/bất kỳ) vào phiên mới với repo agenticmatix.
> Đây là chưng cất từ 2 ngày làm việc thật với lupca (2026-07-31 → 08-01):
> ~30 bug tìm-và-sửa, 6 feature ship qua chính hệ thống, 554 test xanh.
> Đọc file này TRƯỚC, rồi `docs/spec/01..07` khi cần chi tiết.

## 0. Người bạn đang làm việc cùng

lupca — tiếng Việt, vai quản lý (không đọc sâu code, vì thế mới xây hệ này).
Kỳ vọng đã nói rõ nhiều lần:
- **Sửa luôn, đừng hỏi** với việc thuận chiều; chỉ hỏi khi quyết định thiết kế
  thật sự thuộc về user (và khi hỏi: đưa phương án + khuyến nghị).
- **Ghét im lặng**: gate/câu hỏi phải thành CÂU HỎI ở cuối câu trả lời, không
  chôn trong báo cáo. Hệ đã có máy nhắc `pending_approvals` — đừng phá nó.
- **Tiết kiệm**: KHÔNG dùng fable (Agent tool lẫn executor `@claude-fable`);
  review đừng dùng claude (hết token) → dùng `@gpt-5.6-luna-high`; agy cấm
  executor/reviewer (scratch-dir + rubber-stamp — CTV2-220).
- **DB là source of truth duy nhất**: mọi bug/feature/quyết định ghi thành
  task CTV2-xxx trong DB (repo md `~/projects/control-tower` đã bỏ, chỉ là
  snapshot legacy). Xong việc → đánh done trong DB.
- Commit + push thẳng `main`, gh account `lupca`, message nói VÌ SAO.
- Báo cáo trung thực nguyên văn: failed là failed; khoe cả cái mình làm hỏng.

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
Reviewer rẻ + tốt: `@gpt-5.6-luna-high`. Nghi rubber-stamp (pass 4/4 quá nhanh,
0 findings cho diff to)? → tự verify kỹ hơn trước khi approve; đã từng phải
reject verdict rởm của agy.

## 5. Họ bug đặc trưng của codebase này (nghi NGAY khi thấy triệu chứng)

| Triệu chứng | Họ bug | Vết cũ |
|---|---|---|
| Tool "nuốt" tham số, hành vi rơi về default | Mapping tay JSON→args trong `execute_tool` (command_router ~400) vứt field không khai | CTV2-233 (decision), 237 (patch), 228 (agent_id) |
| Constraint DB nổ khó hiểu lúc chuyển trạng thái | `autoflush=False` + raw UPDATE / deferred trigger — flush trước CAS; `emit_task_event` TỰ COMMIT, cấm gọi giữa apply | CTV2-214, flush-CAS, landing-event |
| Run dài tự chết "no progress" | CLI im lặng (`claude -p`) vs watchdog; setting 2400s là tạm | CTV2-232 |
| UniqueViolation seq/attempt khi retry | Retry hygiene chưa xong — run mới (id mới) là sạch | CTV2-219 (MỞ) |
| Task kẹt ở trạng thái lỡ cỡ | Đường cancel/fail nào đó chưa qua orchestration | CTV2-231 (MỞ) |
| "Đã sửa mà vẫn thế" | Server/worker cũ còn chạy — check ps start time | nhiều lần |

Nguyên tắc khi sửa hệ: thêm field vào ToolSpec schema thì PHẢI thêm vào mapping
+ test e2e tầng MCP (pattern trong `tests/test_mcp_native.py`).

## 6. Bản đồ chân lý

- `docs/spec/01..07` — đặc tả sống, PHẢI cập nhật cùng commit khi đổi hành vi.
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
