# 04 — Tool surface (MCP native)

Nguồn chân lý: `TOOL_REGISTRY` trong `backend/app/services/tool_registry.py`
(ADR-001 — mỗi tool một `ToolSpec`, mọi thứ khác là projection).
Auth: Bearer token (issue bằng `scripts/issue-coordinator-token.sh`); role
`coordinator` mở hết, role `executor` bị scope: **mọi call phải mang
`task_id` == token.task_id** (`_task_scope_ok`) — tool executor-callable nào
thiếu `task_id` trong schema là tự khóa chính mình (bài học CTV2-227 F1).

Envelope kết quả: `{ok, data, error{code,message}, next, pending_approvals?}`.

## Vòng đời task

| Tool | Ghi chú / quirks |
|---|---|
| `create_task` | CHỈ nhận `title`, `project`, `depends_on`. Muốn plan/AC/priority/tags → `update_task` sau. Id tự sinh từ counter. |
| `update_task` | Patch CHỈ cho: `acceptance_criteria`, `plan`, `priority`, `tags`. KHÔNG nhận raw_input/files/tests/risk/mode (mode qua gate — CTV2-224 backlog). |
| `generate_spec_plan` | `{task_id, agent_id}` — agent role spec_plan (CLI hoặc API). Ghi AC+plan+files+tests+risk vào task. Retry 1 lần khi JSON sai schema. |
| `dispatch_task` | `{task_id, agent_id?}` — đòi status todo + có AC (hoặc legacy_no_ac). Supervised → gate pending. agent_id bị matcher ghi đè khi approve (CTV2-228). |
| `request_review` | Chỉ khi awaiting-review VÀ chưa có gate review_order mở (driver thường tạo sẵn — approve cái đó thay vì gọi tool này). |
| `record_verdict` | CHỈ reviewer của review run thành công mới được gọi; coordinator không tự verdict hộ. |
| `approve_gate` | `{gate_record_id | task_id | "admin:<id>", decision: approved|rejected}` — xem 03. |
| `cancel_task`, `archive_task` | archive lọc khỏi mọi mặt tiền + đóng luôn gate/escalation của nó. |
| `wait_for_task` | Long-poll (timeout 5–120s, cursor `since_event_id`): trả `{task, changed, events, cursor, latest_run}` ngay khi status đổi / terminal / awaiting_approval / có event mới. Thay cho polling get_status 15s. |
| `get_status` | Không id → list gần nhất. Báo cáo NGUYÊN VĂN — failed là failed. |
| `get_task_events`, `get_run_output` | Event cursor / output chunks replayable. |

## Ngữ cảnh & tri thức

| Tool | Ghi chú |
|---|---|
| `save_project_context` | Executor-callable. Args: `task_id` (BẮT BUỘC — scope), `project_id`, `context_md` (≤150 dòng), `rules` (≤5, name/globs/content; globs = list of strings; name unique, ≤100 ký tự). Từ chối cross-project: task phải thuộc project_id. Thay TRỌN BỘ rules cũ. |
| `get_minimal_context`, `get_impact_radius` | Proxy sang code-review-graph. |
| `manage_knowledge` | CRUD knowledge_items qua admin gate. |
| `compact_context` | Nén context session. |

## Admin & truy vấn

| Tool | Ghi chú |
|---|---|
| `manage_project` / `manage_agent` | create/update/archive/disable qua admin gate. Update nhận `{id, patch}`. API agent đòi api_key khi approve create. |
| `update_settings` | `{key, value}` trong SETTINGS_WHITELIST → admin gate. |
| `query_db` | Raw SQL read-only (1 câu SELECT/WITH), chạy bằng `ct_readonly_user`, cap 500 rows + statement timeout. Bảng mới phải được GRANT (đã có default privileges). |
| `get_stats` | Token/cost/run stats từ LLMUsage. |
| `suggest_agents` | AgentSuggester — xếp hạng theo capabilities/success_rate. |

## Bẫy mapping đã biết

`execute_tool` map JSON args → chuỗi args thủ công cho từng tool
(`command_router.py` ~400). Field nào không được map là bị VỨT LẶNG LẼ —
đã dính 2 lần (approve_gate.decision CTV2-233; manage_agent patch CTV2-237).
Thêm field mới vào schema thì PHẢI thêm vào mapping, và nên viết test e2e
tầng MCP (pattern trong `tests/test_mcp_native.py`).
