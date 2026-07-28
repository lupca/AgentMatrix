# Event & Notification Architecture

## Status: Draft - Issues Documented, Implementation Pending

## Context

Real-time notification system cần thông báo cho user khi:
- Task hoàn thành/fail
- Gate cần approval
- Agent run status changes

---

## Current Problems

### 1. Two Parallel Event Systems
- `publish_event()` - direct WebSocket (chỉ work trong API process)
- `publish_task_event()` - Redis pub/sub (cross-process)

**Impact:** Inconsistent event delivery, khó maintain

### 2. Silent Event Drop
```python
def publish_event(message: dict) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # SILENT DROP - worker không có event loop
```

**Impact:** Events từ worker process bị mất hoàn toàn

### 3. Session Mismatch (Critical)

**Root cause:**
```python
def get_or_create_global_session(self):
    # 1. Tìm BẤT KỲ active global session, sort by pinned + last_activity
    db_session = query.filter(context_level="global", status="active").first()
    
    # 2. Nếu có → return nó (UUID random, không phải session user đang xem)
    if db_session is not None:
        return db_session
    
    # 3. CHỈ KHI không có session nào → tạo id="global"
    db_session = SessionModel(id="global", ...)
```

**Flow thực tế:**
1. User mở Global Chat → `useSessions` tạo session mới (UUID: `abc123`)
2. User dispatch task → task runs in worker
3. Task completes → `record_task_rollup()` gọi `get_or_create_global_session()`
4. Function trả về session `xyz789` (session gần nhất, KHÔNG PHẢI `abc123`)
5. Rollup ghi vào `xyz789`, user đang xem `abc123` → **không thấy gì**

**Impact:** User không bao giờ thấy notification trong chat đang xem

### 4. WebSocket Not Proxied (FIXED)
- Vite/Nginx không proxy `/ws/` endpoint
- Frontend connect sai server
- **Fixed in commit e7f35d2**

### 5. Sync Redis in Async Context
```python
async def _redis_subscriber():
    while True:
        # BLOCKING CALL trong async function
        message = pubsub.get_message(timeout=1.0)
        await asyncio.sleep(0.1)  # Polling, không phải true subscription
```

**Impact:** Inefficient, có thể miss messages

### 6. No Event Filtering
- Frontend nhận TẤT CẢ events từ WebSocket
- Không filter theo session/task/project context

**Impact:** Unnecessary bandwidth, potential confusion

---

## Design Options for Session Mismatch

### Option A: Single Dedicated Inbox
- Mark 1 session as "inbox" (e.g., pinned=true, hoặc special flag)
- All rollups go to inbox
- User phải xem inbox để thấy notifications

**Pros:** Simple, predictable
**Cons:** User phải switch session để xem

### Option B: Rollup to Active Session
- Frontend gửi `session_id` khi dispatch task hoặc qua WebSocket subscription
- Rollup ghi vào session user đang xem

**Pros:** Best UX - notification hiện ngay trong chat đang xem
**Cons:** Cần modify frontend + API

### Option C: Broadcast to All Global Sessions
- Rollup ghi vào TẤT CẢ active global sessions

**Pros:** User luôn thấy notification dù ở session nào
**Cons:** Duplicate data, complex cleanup

### Option D: Separate Notification System
- Không mix notification với chat messages
- UI riêng: bell icon, toast, notification panel

**Pros:** Clean separation of concerns
**Cons:** Different UX pattern, more frontend work

**Recommendation:** Option B hoặc D

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
