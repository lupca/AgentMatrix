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
| `generate_spec_plan` | `{task_id, agent_id}` — chỉ agent CLI; API agent bị từ chối rõ ràng. CLI chạy với `cwd=Project.repo_root` và bắt buộc đọc read-only README/docs/entry points/source liên quan trước khi plan. Prompt nhận FULL raw_input + project context/rules, giữ Scope in/out + bước verb-first + AC objectively-verifiable. Strict output v1.1 bắt buộc `spec_clarity` + `open_questions`; retry 1 lần khi JSON sai. Còn câu hỏi hoặc clarity != high → `spec_questions_pending`, task escalation và approval prompt liệt kê đủ câu hỏi. High + rỗng → clear escalation, `spec_plan_generated`. Mỗi generate ghi metric clarity + question count. |
| `dispatch_task` | `{task_id, agent_id?}` — đòi status todo + có AC (hoặc legacy_no_ac), đồng thời chặn execute khi `open_questions` còn phần tử. Supervised → gate pending. agent_id bị matcher ghi đè khi approve (CTV2-228). |
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
| `compact_context` | Nén context session. |

## Admin & truy vấn

| Tool | Ghi chú |
|---|---|
| `manage_project` / `manage_agent` | create/update/archive/disable qua admin gate. Update nhận `{id, patch}`. API agent đòi api_key khi approve create. |
| `update_settings` | `{key, value}` trong SETTINGS_WHITELIST → admin gate. |
| `query_db` | Raw SQL read-only (1 câu SELECT/WITH), chạy bằng `ct_readonly_user`, cap 500 rows + statement timeout. Bảng mới phải được GRANT (đã có default privileges). |
| `get_stats` | Token/cost/run stats từ LLMUsage. |
| (bảng `tool_metrics`) | Telemetry công cụ tiết-kiệm-token: graph calls + review results (findings, AC pass/fail, tests). Truy vấn qua query_db. Reviewer được prompt chạy `.claude/review-toolchain.md` (ocr...) — thiếu binary = ghi chú và đi tiếp, không phải lỗi. |
| `suggest_agents` | AgentSuggester — xếp hạng theo capabilities/success_rate. |

## Bẫy mapping đã biết

`execute_tool` map JSON args → chuỗi args thủ công cho từng tool
(`command_router.py` ~400). Field nào không được map là bị VỨT LẶNG LẼ —
đã dính 2 lần (approve_gate.decision CTV2-233; manage_agent patch CTV2-237).
Thêm field mới vào schema thì PHẢI thêm vào mapping, và nên viết test e2e
tầng MCP (pattern trong `tests/test_mcp_native.py`).
