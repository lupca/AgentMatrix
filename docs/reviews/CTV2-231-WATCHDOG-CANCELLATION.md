# CTV2-231 — Watchdog cancellation audit

Ngày đo: 2026-08-04.

## Kết luận

Hai đường tạo orphan đã được xác định trong code:

1. Brake/no-progress ở đầu worker chỉ ghi `AgentRun.cancelled` rồi return.
2. `cancel_run` chỉ reset execute Task `dispatched → todo`; review Task vẫn
   `in-review`.

Sau fix, watchdog cancellation đi qua failure transition theo `AgentRun.kind`:
review về `awaiting-review`, execute về `failed`. Cancel review chủ động cũng
dùng `record_review_failure`. Verdict gate bị reject về `awaiting-review` (fix
đã có từ CTV2-1363), và native MCP trả `next` là gọi `request_review`.

## Truy vấn rà soát production

Chạy truy vấn này bằng tool MCP `query_db`, không chạy `psql`/shell. Nó tìm Task
đang ở trạng thái cần một run/gate hoạt động nhưng không còn cả hai; các cột
latest run giúp phân biệt watchdog cancel với dữ liệu legacy chưa từng dispatch.

```sql
WITH open_gates AS (
    SELECT g.task_id
    FROM gate_records AS g
    WHERE g.status = 'pending'
      AND NOT EXISTS (
          SELECT 1
          FROM gate_records AS decision
          WHERE decision.parent_id = g.id
      )
    GROUP BY g.task_id
),
active_runs AS (
    SELECT ar.task_id
    FROM agent_runs AS ar
    WHERE ar.status IN ('queued', 'running')
    GROUP BY ar.task_id
),
latest_run AS (
    SELECT DISTINCT ON (ar.task_id)
           ar.task_id,
           ar.id AS run_id,
           ar.kind,
           ar.status AS run_status,
           ar.error_message,
           ar.completed_at
    FROM agent_runs AS ar
    ORDER BY ar.task_id, ar.created_at DESC, ar.id DESC
)
SELECT t.id,
       t.status,
       t.current_gate,
       t.executor,
       t.reviewer,
       t.error AS task_error,
       lr.run_id,
       lr.kind AS latest_run_kind,
       lr.run_status AS latest_run_status,
       lr.error_message AS latest_run_error,
       lr.completed_at AS latest_run_completed_at,
       t.updated_at
FROM tasks AS t
LEFT JOIN active_runs AS active ON active.task_id = t.id
LEFT JOIN open_gates AS gate ON gate.task_id = t.id
LEFT JOIN latest_run AS lr ON lr.task_id = t.id
WHERE t.archived_at IS NULL
  AND t.status IN ('dispatched', 'in-review')
  AND active.task_id IS NULL
  AND gate.task_id IS NULL
ORDER BY t.updated_at, t.id;
```

## Bằng chứng dữ liệu đã cung cấp

- `CTV2-010`: `in-review` từ 2026-07-26, 0 AgentRun, 0 gate pending.
- `CTV2-069`: `in-review` từ 2026-07-27, 0 AgentRun, 0 gate pending.
- Cả hai đã phải archive thủ công ngày 2026-08-04, nên truy vấn trên loại chúng
  qua `archived_at IS NULL`.
- `CTV2-1360` tái hiện nhánh verdict gate rejected: `in-review`, không active
  run, không pass verdict; `request_review` bị conflict. Contract hiện tại đưa
  nhánh này về `awaiting-review` và đã có regression test request review lại.

Executor của task này không có MCP credential/live `query_db`, nên không tuyên
bố một con số production mới ngoài bằng chứng đo sẵn ở trên. Sau deploy, chạy
nguyên truy vấn qua coordinator MCP; kết quả mong đợi là 0 row. Bất kỳ row nào
còn lại là dữ liệu orphan cũ cần triage/archive, không được sửa trực tiếp bằng
SQL.
