# Polling Notification Architecture

## Status: Implemented (CTV2-114)

## Context

Hệ thống notification hiện tại dùng WebSocket + Redis pub/sub có nhiều vấn đề:
- Session mismatch: rollup ghi vào sai session
- Silent event drop: worker không có event loop
- Hai hệ thống song song: `publish_event()` vs `publish_task_event()`

User insight: Session chat bị xoá sau khi dùng, quan trọng là task/document được lưu lại.

## Decision

**Thay thế WebSocket notification bằng Polling architecture:**
- Tạo `task_events` table làm single source of truth
- Frontend poll `/api/events` định kỳ (10s)
- Giữ SSE streaming cho agent stdout (cần realtime)
- Giữ Redis pub/sub cho cancel propagation

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        TASK LIFECYCLE                           │
│                                                                 │
│   dispatch → running → done/failed                              │
│                ↓                                                │
│          gate_pending → gate_passed/rejected                    │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                     task_events TABLE                           │
│              (Single Source of Truth)                           │
├─────────────────────────────────────────────────────────────────┤
│  - Mọi state change đều ghi vào đây                             │
│  - Indexed by created_at cho polling                            │
│  - LLM context đọc từ đây                                       │
│  - Frontend poll từ đây                                         │
└─────────────────────────────────────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ Frontend │    │   LLM    │    │  Audit   │
    │ Polling  │    │ Context  │    │   Log    │
    └──────────┘    └──────────┘    └──────────┘
```

## Database Schema

```sql
CREATE TABLE task_events (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(20) NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    event_type VARCHAR(30) NOT NULL,
    payload JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
    
    -- Optional: for cleanup/archival
    consumed_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX idx_task_events_created_at ON task_events(created_at);
CREATE INDEX idx_task_events_task_id ON task_events(task_id);
CREATE INDEX idx_task_events_type_created ON task_events(event_type, created_at);
```

### Event Types

| event_type | Trigger | Payload |
|------------|---------|---------|
| `dispatched` | Task được dispatch | `{run_id, agent, cli, command}` |
| `running` | Agent bắt đầu chạy | `{run_id, pid}` |
| `done` | Task hoàn thành | `{run_id, result_ref, exit_code}` |
| `failed` | Task thất bại | `{run_id, error, exit_code}` |
| `cancelled` | User cancel | `{run_id, cancelled_by}` |
| `gate_pending` | Gate cần approval | `{gate, gate_record_id}` |
| `gate_passed` | Gate được approve | `{gate, approved_by}` |
| `gate_rejected` | Gate bị reject | `{gate, rejected_by, reason}` |
| `retrying` | Retry attempt | `{run_id, attempt, max_attempts}` |

## API Design

### Poll Events

```
GET /api/events?since={cursor}&task_id={optional}&types={optional}

Query params:
  - since: ISO timestamp cursor (required)
  - task_id: Filter by specific task (optional)
  - types: Comma-separated event types (optional)

Response:
{
  "events": [
    {
      "id": 123,
      "task_id": "CTV2-098",
      "event_type": "done",
      "payload": {"run_id": "abc", "result_ref": "abc..def"},
      "created_at": "2026-07-28T10:00:00Z"
    }
  ],
  "cursor": "2026-07-28T10:00:01Z",
  "has_more": false
}
```

### Frontend Polling

```typescript
// hooks/useTaskEvents.ts
function useTaskEvents(taskId?: string) {
  const [cursor, setCursor] = useState(() => 
    new Date(Date.now() - 60000).toISOString() // Last 1 minute
  );
  
  const { data } = useQuery({
    queryKey: ['task-events', cursor, taskId],
    queryFn: () => fetchEvents({ since: cursor, taskId }),
    refetchInterval: 10_000, // Poll every 10s
  });
  
  useEffect(() => {
    if (data?.cursor) {
      setCursor(data.cursor);
    }
  }, [data]);
  
  return data?.events ?? [];
}
```

## LLM Context Integration

### Thay thế record_task_rollup()

```python
# TRƯỚC: coordinator.py
def record_task_rollup(self, task_id: str, status: str, ...):
    """Ghi vào session.messages (JSONB array)"""
    message = {"kind": "task_rollup", "content": json.dumps(payload)}
    # Upsert vào global_session.messages
    
# SAU: task_events_service.py
def emit_task_event(task_id: str, event_type: str, payload: dict):
    """Ghi vào task_events table"""
    db.add(TaskEvent(
        task_id=task_id,
        event_type=event_type,
        payload=payload,
    ))
    db.commit()
```

### Context Builder đọc từ task_events

```python
# context_hierarchy.py
def _get_recent_task_events(self, task_id: str, since: datetime) -> list[dict]:
    """Đọc events gần đây cho LLM context"""
    events = db.query(TaskEvent).filter(
        TaskEvent.task_id == task_id,
        TaskEvent.created_at > since,
    ).order_by(TaskEvent.created_at).all()
    
    return [
        {
            "type": e.event_type,
            "payload": e.payload,
            "at": e.created_at.isoformat(),
        }
        for e in events
    ]

def build_messages(self, ...):
    # ... existing code ...
    
    # Inject recent task events vào context
    if task_id:
        recent_events = self._get_recent_task_events(
            task_id, 
            since=last_llm_turn_at
        )
        if recent_events:
            messages.append({
                "role": "system",
                "content": f"Recent task events:\n{json.dumps(recent_events)}"
            })
```

## Migration Plan

### Phase 1: Database + Service (CTV2-098a)
- [ ] Tạo Alembic migration cho `task_events` table
- [ ] Tạo `TaskEvent` SQLAlchemy model
- [ ] Tạo `TaskEventService.emit()` function
- [ ] Unit tests

### Phase 2: Replace Publishers (CTV2-098b)
- [ ] `agent_runner.py`: Thay `_record_task_rollup()` → `emit_task_event()`
- [ ] `task_orchestration.py`: Thay `_notify_gate_pending()` → `emit_task_event()`
- [ ] `dispatch.py`: Emit `cancelled` event
- [ ] Remove `publish_event()`, `publish_task_event()`

### Phase 3: API + Frontend (CTV2-098c)
- [ ] Tạo `GET /api/events` endpoint
- [ ] Frontend: `useTaskEvents` hook
- [ ] Notification badge/panel UI
- [ ] Remove WebSocket connection (giữ SSE cho agent output)

### Phase 4: LLM Context (CTV2-098d)
- [ ] Update `context_hierarchy.py` đọc từ `task_events`
- [ ] Remove rollup từ `session.messages`
- [ ] Integration tests

## What Changes

| Component | Before | After |
|-----------|--------|-------|
| Notification storage | `session.messages` JSONB | `task_events` table |
| Frontend delivery | WebSocket broadcast | Polling `/api/events` |
| LLM context | Replay session messages | Query task_events |
| Gate notification | `publish_event()` (broken) | `emit_task_event()` |
| Task rollup | `record_task_rollup()` | `emit_task_event("done")` |

## What Stays

| Component | Reason |
|-----------|--------|
| SSE `/api/runs/{id}/stream` | Agent stdout cần realtime |
| Redis `agent_run:{id}:output` | Cross-process streaming |
| Redis `agent_run:{id}:cancel` | Cancel propagation |
| `advance_task()` Dramatiq | Orchestration driver |

## Không còn vấn đề

| Issue | Tại sao hết |
|-------|-------------|
| Session mismatch | Không dùng session.messages nữa |
| Silent event drop | Ghi DB trực tiếp, không cần event loop |
| Two parallel systems | Chỉ còn 1: `emit_task_event()` |
| WebSocket complexity | Bỏ WebSocket, dùng polling |

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Polling load | Index on `created_at`, query chỉ lấy since cursor |
| 10s delay | Chấp nhận được cho notifications, SSE vẫn realtime cho output |
| Event table growth | `consumed_at` + periodic cleanup job |

## References

- [Event Notification Architecture (old)](./event-notification-architecture.md) - Vấn đề đã documented
- [ADR-001](../adr/ADR-001-unified-tool-architecture.md) - Tool architecture
