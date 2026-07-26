# Frontend UX Strategy - Control Tower V2

**Document ID:** CTV2-035  
**Date:** 2026-07-26  
**Status:** Research Complete

---

## 1. Current State Analysis

### 1.1 Frontend Architecture

| Layer | Technology | Files |
|-------|------------|-------|
| Framework | React 18 + TypeScript | `frontend/src/` |
| Routing | react-router-dom | `App.tsx` |
| State | Zustand (minimal) | `lib/store.ts` |
| API | Custom fetch wrapper | `lib/api.ts` |
| Styling | Tailwind CSS | Dark theme only |

### 1.2 Component Structure

```
frontend/src/
├── pages/           # Route pages (7 pages)
│   ├── Dashboard.tsx
│   ├── Projects.tsx, ProjectDetail.tsx
│   ├── Agents.tsx, AgentDetail.tsx
│   ├── Tasks.tsx, TaskDetail.tsx
│   └── Kanban.tsx
├── components/      # Reusable UI
│   ├── chat/        # ChatPanel, ChatInput, ChatMessage
│   ├── task/        # TaskHeader, TaskSpec, TaskMeta
│   ├── dashboard/
│   ├── kanban/
│   └── projects/
├── lib/
│   ├── api.ts       # REST client
│   └── store.ts     # Zustand (darkMode, user, activeTaskId only)
└── types/           # TypeScript interfaces
```

### 1.3 Backend Services (for frontend integration)

| Service | Endpoint | Protocol | Purpose |
|---------|----------|----------|---------|
| Tasks CRUD | `/api/tasks/*` | REST | Task lifecycle |
| Chat | `/api/chat` | SSE (fetch) | AI copilot streaming |
| Agent Output | `/api/runs/{id}/stream` | SSE (EventSource) | Live agent output |
| Dispatch | `/api/dispatch` | REST | Trigger agent runs |

### 1.4 Background Infrastructure

- **Dramatiq workers** (`backend/app/workers/agent_runner.py`): Process agent runs
- **Redis Pub/Sub**: Real-time output streaming
- **PostgreSQL**: Durable storage with output chunks

### 1.5 Current Streaming Implementation

**Chat (works):**
- Uses `fetch()` with manual SSE parsing
- Accumulates chunks, updates React state
- Location: `ChatPanel.tsx:119-228`

**Agent Output (not connected):**
- Backend ready: `/api/runs/{run_id}/stream`
- Supports `Last-Event-ID` for resume
- Uses heartbeat for connection health
- **Frontend consumer: NOT IMPLEMENTED**

---

## 2. Gap Analysis

### 2.1 Missing Features (Critical)

| Gap | Impact | Priority |
|-----|--------|----------|
| No live agent output in UI | Users can't see agent progress | P0 |
| No task status auto-refresh | Stale data shown | P0 |
| No dispatch button wired | Can't trigger agents from UI | P0 |
| Chat not connected to tasks/{id}/messages | History not persisted | P1 |

### 2.2 Missing Features (Important)

| Gap | Impact | Priority |
|-----|--------|----------|
| No error boundary | Crashes unmount whole app | P1 |
| No toast notifications | Silent failures | P1 |
| No retry logic in API client | Transient failures break UX | P2 |
| No loading skeletons | Jarring transitions | P2 |
| No keyboard shortcuts | Power users slowed | P3 |

### 2.3 Technical Debt

| Issue | Location | Severity |
|-------|----------|----------|
| Zustand store too minimal | `lib/store.ts` | Medium |
| Each page fetches independently | All pages | Medium |
| No shared SSE connection manager | N/A | High |
| Types duplicated between files | `types/*.ts` | Low |

### 2.4 Goal Alignment

| Goal | Current State | Gap |
|------|---------------|-----|
| Reduce token usage ~80% | Backend optimized, frontend not surfacing | Need output streaming |
| Improve task quality | Review gates in backend | Need verdict UI |
| Real-time visibility | SSE infra ready | Need consumer components |

---

## 3. Proposed Architecture

### 3.1 New Component Tree

```
frontend/src/
├── lib/
│   ├── api.ts              # Keep
│   ├── store.ts            # Expand with task/run slices
│   ├── sse/
│   │   ├── useAgentStream.ts   # Hook for agent output
│   │   ├── useTaskEvents.ts    # Hook for task updates
│   │   └── SSEManager.ts       # Shared connection pool
│   └── hooks/
│       ├── useTasks.ts         # React Query wrapper
│       └── useAgentRuns.ts     # Run state management
├── components/
│   ├── agent-output/
│   │   ├── AgentTerminal.tsx   # ANSI-aware terminal view
│   │   ├── OutputControls.tsx  # Pause/resume/scroll
│   │   └── ProgressIndicator.tsx
│   ├── task/
│   │   ├── DispatchButton.tsx  # NEW: trigger agent
│   │   ├── VerdictPanel.tsx    # NEW: review UI
│   │   └── TaskTimeline.tsx    # NEW: gate progression
│   └── common/
│       ├── ErrorBoundary.tsx
│       ├── ToastProvider.tsx
│       └── Skeleton.tsx
└── pages/
    └── TaskDetail.tsx          # Integrate AgentTerminal
```

### 3.2 SSE Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  SSEManager │────▶│ EventSource  │────▶│ Redis Pub/  │
│  (singleton)│     │ per run_id   │     │    Sub      │
└──────┬──────┘     └──────────────┘     └─────────────┘
       │
       ▼
┌──────────────────┐
│ useAgentStream() │  React hook
│  - subscribe     │
│  - unsubscribe   │
│  - onLine()      │
│  - onStatus()    │
└──────────────────┘
```

**Key Design Decisions:**
1. Single SSEManager instance manages all EventSource connections
2. Automatic reconnect with Last-Event-ID
3. Connection pooling (max 6 concurrent per origin)
4. React hooks abstract subscription lifecycle

### 3.3 State Management Enhancement

```typescript
// Expanded Zustand store slices
interface AppStore {
  // Existing
  darkMode: boolean;
  user: User | null;
  
  // NEW: Active runs tracking
  activeRuns: Map<string, RunState>;
  subscribeToRun: (runId: string) => void;
  unsubscribeFromRun: (runId: string) => void;
  
  // NEW: Task cache for optimistic updates
  taskCache: Map<string, Task>;
  updateTaskOptimistic: (id: string, patch: Partial<Task>) => void;
}
```

### 3.4 Data Flow

```
User clicks Dispatch
        │
        ▼
POST /api/dispatch ──────▶ Returns run_id
        │
        ▼
SSEManager.subscribe(run_id)
        │
        ▼
EventSource(/api/runs/{run_id}/stream)
        │
        ├──▶ type: history (replay)
        ├──▶ type: stdout  (live lines)
        ├──▶ type: status  (running/done/failed)
        └──▶ type: done    (cleanup)
```

---

## 4. Implementation Roadmap

### Phase 1: Core Streaming (CTV2-036)
**Scope:** Get agent output visible in TaskDetail

| Task | Effort | Dependency |
|------|--------|------------|
| Create `SSEManager.ts` singleton | 2h | None |
| Create `useAgentStream.ts` hook | 2h | SSEManager |
| Create `AgentTerminal.tsx` component | 3h | useAgentStream |
| Integrate into TaskDetail.tsx | 1h | AgentTerminal |
| **Total** | **8h** | |

### Phase 2: Dispatch Integration (CTV2-037)
**Scope:** Wire up dispatch from frontend

| Task | Effort | Dependency |
|------|--------|------------|
| Create `DispatchButton.tsx` | 1h | None |
| Wire POST /api/dispatch | 1h | Button |
| Auto-subscribe on dispatch success | 1h | Phase 1 |
| Handle dispatch conflicts (409) | 1h | |
| **Total** | **4h** | Phase 1 |

### Phase 3: Task State Sync (CTV2-038)
**Scope:** Real-time task status updates

| Task | Effort | Dependency |
|------|--------|------------|
| Backend: Task change events via Redis | 2h | |
| Frontend: `useTaskEvents.ts` hook | 2h | |
| Auto-refresh task list on changes | 1h | Hook |
| Optimistic UI updates | 2h | Hook |
| **Total** | **7h** | |

### Phase 4: UX Polish (CTV2-039)
**Scope:** Error handling, notifications, skeletons

| Task | Effort | Dependency |
|------|--------|------------|
| Add ErrorBoundary | 1h | None |
| Add ToastProvider + react-hot-toast | 2h | None |
| Add loading skeletons | 2h | |
| Add retry logic to api.ts | 1h | |
| **Total** | **6h** | |

### Phase 5: Review UI (CTV2-040)
**Scope:** Verdict gate visualization

| Task | Effort | Dependency |
|------|--------|------------|
| Create VerdictPanel.tsx | 3h | |
| Create TaskTimeline.tsx | 2h | |
| Wire POST /api/tasks/{id}/verdict | 1h | |
| **Total** | **6h** | Phase 2 |

### Summary Timeline

| Phase | Tasks | Effort | Dependencies |
|-------|-------|--------|--------------|
| Phase 1 | CTV2-036 | 8h | - |
| Phase 2 | CTV2-037 | 4h | Phase 1 |
| Phase 3 | CTV2-038 | 7h | Phase 1 |
| Phase 4 | CTV2-039 | 6h | - |
| Phase 5 | CTV2-040 | 6h | Phase 2 |
| **Total** | | **31h** | |

---

## 5. Library Recommendations

### 5.1 Required Additions

| Library | Purpose | Bundle Size | Alternative |
|---------|---------|-------------|-------------|
| None for SSE | Native EventSource | 0 KB | |

**SSE Strategy:** Use native `EventSource` API. No library needed - the backend already produces standard SSE format. The `fetch()` approach in ChatPanel works but EventSource handles reconnection automatically.

### 5.2 Recommended Additions

| Library | Purpose | Bundle Size | Priority |
|---------|---------|-------------|----------|
| `react-hot-toast` | Notifications | 5 KB | P1 |
| `@tanstack/react-query` | Server state | 12 KB | P2 |
| `xterm.js` | Terminal rendering | 250 KB | P2 (optional) |

**Notes:**
- `react-hot-toast`: Simple, small, zero-config. For dispatch success/failure.
- `@tanstack/react-query`: Dedupes requests, handles cache, stale-while-revalidate. Consider for Phase 3+.
- `xterm.js`: Only if agent output includes ANSI codes. Otherwise use styled `<pre>`.

### 5.3 Not Recommended

| Library | Reason |
|---------|--------|
| Socket.io | Overkill - SSE sufficient for one-way streaming |
| Redux | Zustand already in use, simpler |
| SWR | react-query has better DevTools |

### 5.4 Keep Current

| Library | Status |
|---------|--------|
| Zustand | Keep - lightweight, sufficient |
| react-router-dom | Keep |
| Tailwind CSS | Keep |
| Lucide icons | Keep |

---

## Appendix A: SSEManager Implementation Sketch

```typescript
// lib/sse/SSEManager.ts
class SSEManager {
  private connections = new Map<string, EventSource>();
  private subscribers = new Map<string, Set<(event: SSEEvent) => void>>();

  subscribe(runId: string, onEvent: (event: SSEEvent) => void) {
    if (!this.subscribers.has(runId)) {
      this.subscribers.set(runId, new Set());
      this.connect(runId);
    }
    this.subscribers.get(runId)!.add(onEvent);
    return () => this.unsubscribe(runId, onEvent);
  }

  private connect(runId: string, lastEventId?: number) {
    const url = lastEventId 
      ? `/api/runs/${runId}/stream?last_event_id=${lastEventId}`
      : `/api/runs/${runId}/stream`;
    
    const es = new EventSource(url);
    this.connections.set(runId, es);

    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      this.notify(runId, { ...data, lastEventId: e.lastEventId });
      if (data.type === 'done') {
        this.close(runId);
      }
    };

    es.onerror = () => {
      // Auto-reconnect with last ID after 2s
      setTimeout(() => this.connect(runId, /* stored lastEventId */), 2000);
    };
  }

  private close(runId: string) {
    this.connections.get(runId)?.close();
    this.connections.delete(runId);
  }
}

export const sseManager = new SSEManager();
```

---

## Appendix B: useAgentStream Hook Sketch

```typescript
// lib/sse/useAgentStream.ts
export function useAgentStream(runId: string | null) {
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<'pending' | 'running' | 'done' | 'failed'>('pending');

  useEffect(() => {
    if (!runId) return;

    const unsubscribe = sseManager.subscribe(runId, (event) => {
      switch (event.type) {
        case 'history':
        case 'stdout':
          setLines(prev => [...prev, event.content]);
          break;
        case 'status':
          setStatus(event.status);
          break;
      }
    });

    return unsubscribe;
  }, [runId]);

  return { lines, status };
}
```

---

## Appendix C: References

- Backend SSE: `backend/app/api/stream.py`
- Worker output: `backend/app/workers/agent_runner.py`
- Redis pub/sub: `backend/app/workers/output_streamer.py`
- Current chat SSE: `frontend/src/components/chat/ChatPanel.tsx`
