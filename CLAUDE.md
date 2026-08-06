# AGENTMATRIX (AGMX) V2

Hệ thống điều phối coding agent với gate-based workflow, four-eyes review, và autonomy controls.

## Kiến trúc

```
Coordinator CLI (Outside Repo Workdir)
  └── FastMCP Client (mcp_native) → http://localhost:8100/mcp (Bearer Token Auth)
                                       │
                                       ▼
                              FastMCP Server (Port 8100)
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
              CommandRouter / Services         PostgreSQL (5433)
                        │                      ct_readonly_user
                        ▼
                 Dramatiq Worker → CLIDispatcher → agy/claude/codex
```

## Data Model chính

- **Task**: todo→dispatched→awaiting-review→in-review→done/changes-requested/failed
- **AgentRun**: queued→running→success/failed/timeout (kind: execute|review)
- **GateRecord**: append-only ledger (pending→approved/rejected)
- **Agent**: CLI-backed (claude/agy/codex) với capabilities[], success_rate
- **Session**: context_level (global|project|task), messages[]

## Gate Flow

```
todo → [dispatch gate] → dispatched → [run_agent] → awaiting-review
     → [review_order gate] → in-review → [run_agent review] → [verdict gate] → done
```

Mode: supervised (cần approve), plan-only (block dispatch), bypass (auto-approve)

## Autonomy & Brakes

```python
AutonomyPolicy(per project hoặc global Setting):
  autonomy: supervised|plan-only|auto
  auto_max_risk: low|normal
  auto_max_rounds: 3

check_brakes():
  - autonomy_enabled=false → STOP
  - authoritative API task_cost ≥ max_cost_usd_per_task → STOP
  - task_tokens ≥ max_tokens_per_task → STOP
  - active_runs ≥ max_concurrent_runs → QUEUE
```

## Commands

```bash
# Start backend (DB + Redis + FastMCP + Worker)
./scripts/start-backend.sh

# Run tests
backend/venv/bin/python -m pytest backend/tests -q
```

## Quy tắc quan trọng

- **Four-eyes**: reviewer ≠ executor (DB constraint)
- **GateRecord**: append-only, immutable
- **Worktree isolation**: mỗi AgentRun chạy trong git worktree riêng
- **MCP Native**: Coordinator CLI tương tác 100% qua tool surface FastMCP native
- **Coordinator Workdir**: Chạy coordinator CLI từ ngoài repo (`~/ct-coordinator`)

## Chân lý nằm trong DB, không nằm trong file

Dự án này tự lưu đặc tả, kiến thức, ý tưởng và công việc của chính nó trong
`control_tower` (project id `agenticmatix`) và đọc/ghi qua tool surface MCP.
**Đừng tra file markdown rồi tin là hiện trạng** — tra DB. Số đo 2026-08-06:

| Hỏi gì | Tra bằng | Có gì cho agenticmatix |
|---|---|---|
| Hệ PHẢI hành xử thế nào | `spec_get {filter:{project_id:"agenticmatix"}}` | 36 spec_item (13 design, 15 constraint, 6 requirement) |
| Đang làm gì / đã làm gì | `get_status`, `query_db` trên `tasks` | 228 done, 3 todo |
| Bằng chứng, số liệu, nhật ký sự cố | `manage_knowledge {action:"list"}` | 6 knowledge_item (lesson/pattern/reference) |
| Ý tưởng thô chưa thành task | `manage_inbox {action:"list"}` | 24 inbox_item (19 open, 5 triaged) |

**spec_item là hợp đồng, không phải ghi chép.** Mỗi item có `kind`
(design/constraint/requirement), `status` (draft/active/stale), `confidence`,
`anchors` (neo vào `path`+`symbol` thật trong repo) và `task_links`. Trục
**`realization`** (`agreed` → `built`) được **SUY RA** từ anchor + task đã done,
không ai gán tay được. Muốn biết còn nợ gì: `spec_get {filter:{backlog:true}}`.
Muốn biết commit vừa rồi làm lệch cái gì: `spec_stale`.

**Đổi hành vi hệ thống thì cập nhật spec_item trong cùng phiên**, bằng
`spec_write` — `op:"update"` khi làm rõ thêm, `op:"supersede"` khi lời khẳng
định cũ đã sai. Kèm `op:"anchor"` trỏ vào symbol vừa viết và `op:"task_link"`
relation `implements` tới task, nếu không thì trục thực hoá sẽ báo "chưa thành
code" dù code đã land.

Ví dụ có thật: CTV2-1409 sửa phanh chống-lặp của driver → cập nhật spec_item
`526e64f9` (đoạn cũ của nó đang khẳng định một luật THIẾU, chính là luật gây ra
bug), neo `_rounds_since_last_escalation`, link task → `realization` tự chuyển
sang `built`.

Thư mục `docs/` vẫn còn trong repo nhưng là **tài liệu lịch sử, không phải nguồn
chân lý**: khi nó lệch với DB thì DB đúng. Ngoại lệ duy nhất là
`docs/AGENT-PLAYBOOK.md` bên dưới.

## Cho AI agent

**ĐỌC `docs/AGENT-PLAYBOOK.md` TRƯỚC KHI LÀM BẤT CỨ VIỆC GÌ** — đó là ký ức
làm việc chưng cất từ các phiên trước: vòng lặp giải quyết vấn đề, bộ đồ nghề
riêng của dự án, cách điều phối task qua chính hệ thống, các họ bug đặc trưng,
và kỳ vọng của lupca (sửa luôn không hỏi, không dùng fable/claude-review/agy,
DB là source of truth, mọi việc ghi thành task CTV2-xxx).
