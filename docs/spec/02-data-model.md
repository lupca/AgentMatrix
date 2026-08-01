# 02 — Data model & vòng đời

Models: `backend/app/db/models.py`. Migration: alembic (`backend/alembic/versions/`).

## Task

```
todo → dispatched → awaiting-review → in-review → done
                                          └→ changes-requested → (todo → vòng mới)
bất kỳ đâu → failed / cancelled
```

Cột đáng chú ý:
- `status` + `version`: mọi chuyển trạng thái đi qua `_cas_status` — UPDATE
  `WHERE status=:expected AND version=:expected` (optimistic CAS). LƯU Ý: session
  chạy `autoflush=False`, `_cas_status` **flush trước** khi bắn raw UPDATE để
  constraint thấy trọn transition (đã từng dính `ck_tasks_terminal_not_awaiting_approval`).
- `mode`: supervised | plan-only | bypass — resolve lúc tạo bởi `mode_for_task`
  từ setting `autonomy` (+ `auto_max_risk` so với `task.risk`). Setting
  `default_mode` là nút CHẾT (CTV2-222).
- `result_ref`: `"<base>..<head>"` do executor commit thật, validate ancestor
  bằng git. Task không commit thì KHÔNG có đường done (gap CTV2-235).
- `verdict`/`final_verdict`: chỉ được set qua verdict gate từ review run thật;
  trigger DB `trg_tasks_done_verdict` đòi verdict='pass' khi done.
- `awaiting_approval` + `approval_prompt`: cờ escalation/gate chờ human.
- `spec_clarity` (`high|medium|low`, nullable cho task legacy) + `open_questions`
  (JSON, nullable): kết quả SpecPlanResult v1.1. Danh sách còn phần tử chặn
  execute-dispatch cho tới khi coordinator cập nhật `raw_input` và regenerate.
- `archived_at`: soft-delete toàn cục (ArchivableMixin) — mọi query mặt tiền
  phải lọc `archived_at IS NULL` (pending_approvals đã từng quên — đã sửa).
- `legacy_no_ac`: task import cũ, miễn yêu cầu acceptance_criteria khi dispatch.
- id: `<task_prefix>-<seq>` sinh từ `projects.next_task_seq` (counter atomic).
  Import md phải re-seed counter (migrate script đã làm).

## AgentRun

```
queued → running → success / failed / cancelled
```
- `kind`: execute | review. `attempt`: unique `(task_round_id, kind, attempt)` —
  run mới cùng round phải lấy `max(attempt)+1` (review re-order đã sửa).
- `dramatiq_message_id`: PHẢI ghi ngay sau `run_agent.send(...)` — outbox
  publisher coi NULL = chưa gửi và sẽ gửi bản sao (CTV2-212).
- `pid`, `started_at`: reaper dùng để phát hiện worker chết
  (`reap_dead_running_runs`, min age 120s chống PID reuse).
- Output lưu `agent_output_chunks` (đọc bằng tool `get_run_output`).

## TaskRound

Một round mỗi lần execute-dispatch; verdict ghi vào round. `auto_max_rounds`
(default 3) round changes-requested → brake escalate "human replan".

## GateRecord (task gates) & AdminGateRecord (admin gates)

- Append-only (trigger chặn UPDATE). Quyết định = row con `parent_id` → parent.
- Gate mở = `status='pending'` AND childless AND task chưa archive.
- `gate_type`: dispatch | execution | review_order | verdict. Admin:
  `<entity>/<action>` (agents/update, settings/update, ...).
- Đã biết: driver tự tạo gate review_order khi executor xong; `request_review`
  tạo thêm cái TRÙNG (CTV2-230 — nên approve gate driver có sẵn thay vì gọi
  request_review khi task đã awaiting-review).

## Agent

- `agent_type`: `cli` (claude/agy/codex, chạy bằng subscription CLI) hoặc `api`
  (OpenAI-compatible endpoint: `provider` + `model` + `api_key` mã hóa +
  `base_url` — vd SiliconFlow). `role`: executor/reviewer/coordinator/spec_plan.
- `effort`: low/medium/high — bắt buộc với một số model agy (gemini-3.6-flash);
  model name có suffix `-low/-medium/-high/...` thì KHÔNG truyền flag nữa.
- `success_rate`: số đo production — KHÔNG được ghi đè bằng giá trị tĩnh từ md
  (migrate script upsert giữ nguyên).
- `agent_accounts`: health/quota per (agent, cli) — CASCADE khi xóa agent
  (lý do nữa để không bao giờ clear bảng agents).

## Project

- `repo_root` bắt buộc để dispatch. `task_prefix`, `next_task_seq` cho id.
- `context_md` + `context_generated` + quan hệ `project_rules` — xem `06-context-rules.md`.

## Session, TaskEvent, OutboxEvent, LLMUsage

- Session: context_level global|project|task; token MCP nào cũng được cấp một
  session router thật (`_ensure_session`).
- TaskEvent: nguồn cho `get_task_events`/`wait_for_task` (cursor = event id).
- OutboxEvent: pattern outbox cho enqueue run (CTV2-205) — ghi cùng transaction
  với AgentRun, publisher gửi lại nếu crash trước send.
- LLMUsage: token/cost per call — nguồn của `get_stats` và brake cost.

## Nguồn dữ liệu markdown (control-tower repo)

`~/projects/control-tower/` là nguồn import: `index.md` (project registry),
`projects/<id>/<id>.md` + `projects/<id>/tasks/*.md`, `knowledge/agents/@*.md`,
`knowledge/<category>/*.md`. Import bằng `scripts/migrate_md_to_db.py`
(xem `07-runtime-ops.md` — agents là upsert, KHÔNG clear).
