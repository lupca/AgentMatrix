# 04 — Tool surface (MCP native)

Nguồn chân lý: `TOOL_REGISTRY` trong `backend/app/services/tool_registry.py`
(ADR-001 — mỗi tool một `ToolSpec`, mọi thứ khác là projection).
Auth: Bearer token (issue bằng `scripts/issue-coordinator-token.sh`); role
`coordinator` mở hết, role `executor` bị scope: **mọi call phải mang
`task_id` == token.task_id** (`_task_scope_ok`) — tool executor-callable nào
thiếu `task_id` trong schema là tự khóa chính mình (bài học CTV2-227 F1).

Envelope kết quả: `{ok, data, error{code,message}, next, pending_approvals?}`.

## Vòng đời task

| `manage_inbox` | CRUD ý tưởng thô không qua admin gate; `promote` tạo task thật và đánh dấu idea `triaged`. Hỗ trợ add/update/delete/list/promote. |

| Tool | Ghi chú / quirks |
|---|---|
| `create_task` | CHỈ nhận `title`, `project`, `depends_on`. Muốn plan/AC/priority/tags → `update_task` sau. Id tự sinh từ counter. |
| `update_task` | Patch cho: `raw_input` (replace semantics), `acceptance_criteria`, `plan`, `priority`, `tags`; dependency edits giữ nguyên. Dùng `raw_input` để ghi câu trả lời human trước khi regenerate spec. KHÔNG nhận files/tests/risk/mode. |
| `generate_spec_plan` | `{task_id, agent_id?, critic_id?}` — planner và critic đều là CLI độc lập. Planner bắt buộc tra `spec_get` qua MCP trước khi kết luận prior_art/constraints, ưu tiên conflict+constraint → requirement/design → anchor → task link, rồi xuất strict v2.0 gồm acceptance, constraints, evidence có nguồn tái lập, prior_art, ruled_out và limits. Critic bắt buộc tra spec trước khi chấm prior_art, chạy với trần 150k, không nhận diff, chỉ được reject bằng dẫn chứng. Critic reject → `spec_plan_critic_rejected`; accept nhưng còn câu hỏi/clarity != high → `spec_questions_pending`; đủ rõ → `spec_plan_generated`. Nội bộ giờ đi plan→DB→critic (xem 03): plan commit xong mới chạy critic, nên critic lỗi không mất plan — trả `{'error', 'plan_persisted': true, 'next': 'critique_spec_plan'}` thay vì phải chạy lại cả planner. CTV2-1382: cả hai bước chạy trong Dramatiq worker qua outbox, không còn trong tiến trình MCP server; tool chờ tối đa 30s — xong kịp thì trả nguyên payload trên, hết giờ thì trả handle `{run_id, task_id, status, next: 'wait_for_task', latest_run}` (gọi tiếp `wait_for_task`). Đòi `task.status == 'todo'`. |
| `critique_spec_plan` | `{task_id, critic_id?}` — chạy riêng bước critic trên plan đã có sẵn trên task, KHÔNG bao giờ gọi planner. Dùng để retry sau khi critic lỗi hoặc chạy thêm vòng critique sau reject. Mỗi lần gọi append một GateRecord `plan_critic` mới, không có trần cứng số vòng. |
| `dispatch_task` | `{task_id, agent_id?}` — đòi status todo + có ít nhất một mục trong `acceptance_criteria ++ constraints` (hoặc legacy_no_ac), chặn khi còn open_questions hoặc plan generated chưa có critic accept hiện hành. Supervised → gate pending. |
| `request_review` | Chỉ khi awaiting-review VÀ chưa có gate review_order mở (driver thường tạo sẵn — approve cái đó thay vì gọi tool này). Reviewer chỉ định được giữ nguyên; nếu không tồn tại/disabled/trùng executor thì fail kèm 2–3 gợi ý hợp lệ, không âm thầm thay. |
| `record_verdict` | CHỈ reviewer của review run thành công mới được gọi; coordinator không tự verdict hộ. |
| `approve_gate` | `{gate_record_id | task_id | "admin:<id>", decision: approved|rejected}` — xem 03. |
| `cancel_task`, `archive_task` | archive lọc khỏi mọi mặt tiền + đóng luôn gate/escalation của nó. |
| `wait_for_task` | Long-poll (timeout 5–120s, cursor `since_event_id`): bỏ cursor → snapshot `MAX(TaskEvent.id)` lúc vào và chỉ event phát sinh sau đó mới đánh thức; cursor tường minh (kể cả `0`) giữ replay semantics cũ. Trả `{task, changed, events, cursor, latest_run}` ngay khi status đổi / terminal / awaiting_approval / có event mới; timeout không đổi trả `changed=false` với effective cursor. Thay cho polling get_status 15s. |
| `get_status` | Không id → list gần nhất. Báo cáo NGUYÊN VĂN — failed là failed. |
| `get_task_events`, `get_run_output` | Event cursor / output chunks replayable. |

## Ngữ cảnh & tri thức

| Tool | Ghi chú |
|---|---|
| `save_project_context` | Executor-callable. Args: `task_id` (BẮT BUỘC — scope), `project_id`, `context_md` (≤150 dòng), `rules` (≤5, name/globs/content; globs = list of strings; name unique, ≤100 ký tự). Từ chối cross-project: task phải thuộc project_id. Thay TRỌN BỘ rules cũ. |
| `get_minimal_context`, `get_impact_radius` | Proxy sang code-review-graph. Cần session scope project/task. MỌI call graph ghi 1 row `tool_metrics` (ok/fail/cache-hit, duration, result_count, bytes_out) — fail vẫn fallback [] không chặn task, nhưng giờ đo được (CTV2-239). |
| `manage_knowledge` | CRUD knowledge_items qua admin gate. |
| `spec_get` | MCP native expose trực tiếp; đọc theo ids/filter/task_id và trả item + relation + `spec_anchor` + `spec_task_link` ở cả top-level/per-item. |
| `compact_context` | Nén context session. |

## Admin & truy vấn

| Tool | Ghi chú |
|---|---|
| `manage_project` / `manage_agent` | create/update/archive/disable qua admin gate. Update nhận `{id, patch}`. API agent đòi api_key khi approve create. Agent roles: `executor`/`reviewer`/`coordinator`/`spec_plan` — truyền `role` (singular, legacy) hoặc `roles` (array, preferred). Capabilities: ~50 giá trị ENUM (code, backend, review, architecture...) — xem `capability_types` table. |
| `update_settings` | `{key, value}` trong SETTINGS_WHITELIST → admin gate. |
| `query_db` | Raw SQL read-only (1 câu SELECT/WITH), chạy bằng `ct_readonly_user`, cap 500 rows + statement timeout. Bảng mới phải được GRANT (đã có default privileges). |
| `get_stats` | Token/cost/run stats từ LLMUsage, cộng tỷ lệ plan bị critic trả và số vòng execute thừa của cohort trước/sau critic. |
| (bảng `tool_metrics`) | Telemetry công cụ tiết-kiệm-token: graph calls + review results (findings, AC pass/fail, tests). Truy vấn qua query_db. Reviewer được prompt chạy `.claude/review-toolchain.md` (ocr...) — thiếu binary = ghi chú và đi tiếp, không phải lỗi. |
| `suggest_agents` | AgentSuggester — xếp hạng theo capabilities/success_rate. |

## Bẫy mapping đã biết

`execute_tool` map JSON args → chuỗi args thủ công cho từng tool
(`command_router.py` ~400). Field nào không được map là bị VỨT LẶNG LẼ —
đã dính 2 lần (approve_gate.decision CTV2-233; manage_agent patch CTV2-237).
Thêm field mới vào schema thì PHẢI thêm vào mapping, và nên viết test e2e
tầng MCP (pattern trong `tests/test_mcp_native.py`).
