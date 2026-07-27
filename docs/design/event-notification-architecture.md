# Event & Notification Architecture

## Status: Draft

## Context

Real-time notification system cần thông báo cho user khi:
- Task hoàn thành/fail
- Gate cần approval
- Agent run status changes

## Current Problems

### 1. Two Parallel Event Systems
- `publish_event()` - direct WebSocket (chỉ work trong API process)
- `publish_task_event()` - Redis pub/sub (cross-process)

### 2. Silent Event Drop
```python
def publish_event(message: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # SILENT DROP - worker không có event loop
```

### 3. Session Mismatch
- `record_task_rollup()` ghi vào **fixed global session**
- User chat trong **session riêng** do `useSessions` tạo
- Rollup messages không hiện trong chat user đang xem

### 4. WebSocket Not Proxied (FIXED)
- Vite/Nginx không proxy `/ws/` endpoint
- Frontend connect sai server

## Target Architecture

```
┌─────────────────┐    ┌─────────────────┐
│   API Process   │    │ Worker Process  │
└────────┬────────┘    └────────┬────────┘
         │                      │
         ▼                      ▼
┌─────────────────────────────────────────┐
│         EventBus.publish(event)          │
│         (sync-safe, Redis pub/sub)       │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│      Redis Channel: control_tower:events │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│     AsyncEventSubscriber (API only)      │
│     - Filter by event.scope              │
│     - Broadcast to matching clients      │
└─────────────────────────────────────────┘
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
    [Client A]  [Client B]  [Client C]
    session:x   session:y    global
```

## Event Types

```python
class EventScope:
    type: "global" | "session" | "task" | "project"
    id: str | None

class BaseEvent:
    type: EventType
    timestamp: datetime
    scope: EventScope

# Concrete events:
- GatePendingEvent(task_id, gate_type)
- TaskRollupEvent(task_id, status, session_id)
- TaskStatusChangedEvent(task_id, old_status, new_status)
```

## Module Structure

```
backend/app/events/
├── __init__.py      # Re-exports
├── types.py         # Event models (Pydantic)
├── bus.py           # RedisEventBus (sync-safe publish)
└── subscriber.py    # AsyncEventSubscriber
```

## Implementation Phases

### Phase 1: Fix Immediate Issues
- [x] Add WebSocket proxy to vite.config.ts
- [x] Add WebSocket proxy to nginx.conf
- [ ] Fix session mismatch - rollup should use active session

### Phase 2: Add Event Package
- [ ] Create `backend/app/events/` package
- [ ] Implement sync-safe `publish()` function
- [ ] Implement async Redis subscriber

### Phase 3: Migrate Publishers
- [ ] `coordinator.record_task_rollup()` → `events.publish()`
- [ ] `task_orchestration._notify_gate_pending()` → `events.publish()`

### Phase 4: WebSocket Subscriptions
- [ ] Client sends subscription on connect
- [ ] Server filters events by scope
- [ ] Only deliver relevant events

### Phase 5: Frontend Updates
- [ ] Update `useWebSocket.ts` with subscription protocol
- [ ] Filter events client-side as fallback

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Single Redis channel | Simpler; filtering at delivery |
| Pydantic events | Type safety, validation |
| Sync-safe publisher | Workers are sync |
| Server-side filtering | Reduce bandwidth |

## Verification

1. Start worker, dispatch task
2. Open global chat in browser
3. Task completes → notification appears in chat
4. Reload page → notification still visible
