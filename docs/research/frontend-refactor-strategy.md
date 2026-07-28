# Frontend Refactor Strategy Research

**Task:** CTV2-118  
**Date:** 2026-07-28  
**Status:** Research Complete

---

## Component Audit

### Hotspot Files Analysis (>250 lines)

| File | Lines | Primary Responsibilities | Proposed Splits |
|------|-------|-------------------------|-----------------|
| `ChatPanel.tsx` | 585 | Session mgmt, SSE streaming, WebSocket events, model switching, message rendering | **ChatHeader** (header + controls), **MessageList** (message container + scroll logic), **SSEStreamHandler** (custom hook for streaming logic) |
| `ProjectDetail.tsx` | 403 | Project fetch, task list, filters, settings modal, KPI cards | **ProjectHeader** (banner + nav), **ProjectKpiCards** (4 stat cards), **ProjectTaskList** (table + filters) |
| `TaskTable.tsx` | 391 | Sorting, pagination, row expansion, status badges | **TaskTableRow** (single row + expand drawer), **TaskSortHeader** (sortable column headers) |
| `Dashboard.tsx` | 372 | Stats fetch, token telemetry, KPI assembly, charts | **TokenTelemetryCard** (lines 222-343, the token stats section), move fetch logic to `useDashboardStats` hook |
| `AgentDetail.tsx` | 368 | Agent profile, task filtering by role, KPI cards, task table | **AgentProfileHeader** (lines 118-205), **AgentKpiCards** (lines 207-248), **AgentTaskList** (lines 250-362) |
| `Agents.tsx` | 329 | Agent CRUD, filters, pagination, modal | **AgentFilterBar** (search + role/status filters), **AgentModal** (lines 299-323) |
| `Projects.tsx` | 346 | Project CRUD, filters, modal | **ProjectFilterBar** (search + status filters), **ProjectModal** (lines 263-340) |
| `TaskSpec.tsx` | 298 | Tab navigation, acceptance criteria, files/tests display | **TaskSpecOverview** (lines 98-206), **TaskSpecFiles** (lines 209-278) |

---

## Performance Findings

### Missing Memoization

| File:Line | Issue | Fix |
|-----------|-------|-----|
| `ChatPanel.tsx:81-94` | `checkIfNearBottom`, `scrollToBottom`, `handleScroll` recreated on every render despite being stable | Already uses `useCallback` - OK |
| `ChatPanel.tsx:97-130` | `handleWebSocketMessage` callback is stable but `setMessages` calls inside create new arrays on every WS event | Consider batching or using `useReducer` for message state |
| `ProjectDetail.tsx:79-90` | `filteredTasks` computed inline on every render | Wrap in `useMemo` with `[tasks, search, statusFilter]` deps |
| `TaskTable.tsx:49-59` | `sortedTasks` computed inline on every render | Wrap in `useMemo` with `[tasks, sortField, sortDirection]` deps |
| `TaskTable.tsx:68-105` | `getStatusBadge` and `getPriorityBadge` return new JSX on every call | Extract to stable component: `<StatusBadge status={s} />` |
| `AgentDetail.tsx:82-83` | `executorTasks`/`reviewerTasks` filter inline | Wrap in `useMemo` with `[tasks, id]` deps |
| `AgentDetail.tsx:85-90` | `displayedTasks` filter inline with search | Wrap in `useMemo` |
| `Agents.tsx:120-135` | `filteredAgents` filter inline | Wrap in `useMemo` with `[agents, search, roleFilter, statusFilter]` |
| `Projects.tsx:97-107` | `filteredProjects` filter inline | Wrap in `useMemo` |
| `Dashboard.tsx:162-165` | `maxOperationTokens` computed inline | Wrap in `useMemo` |

### N+1 / Sequential API Calls

| File:Line | Issue | Fix |
|-----------|-------|-----|
| `ProjectDetail.tsx:43-52` | Project fetched, then tasks fetched sequentially | Use `Promise.all([api.get('/projects/${id}'), api.get('/tasks?project=${id}')])` |
| `AgentDetail.tsx:42-60` | Agent → Stats → Tasks fetched sequentially | Parallelize with `Promise.all` |
| `Dashboard.tsx:88-105` | Overview → Projects → Usage → Comparison fetched in parallel (good), but then `projectStats` is fetched separately after (line 101-105) | Move `projectStats` into the initial `Promise.all` |

### Unbounded Lists Without Virtualization

| File:Line | Issue | Fix |
|-----------|-------|-----|
| `ChatPanel.tsx:547-549` | All messages rendered without virtualization | For long chat histories (>100 messages), consider `react-window` or pagination |
| `TaskTable.tsx:195-371` | All paginated tasks rendered (limited by pagination - OK) | Pagination mitigates - low priority |

---

## Code Smell Findings

### Security Issues (from OCR pre-scan)

| File:Line | Severity | Issue | Fix |
|-----------|----------|-------|-----|
| `ProjectDetail.tsx:203-207` | **HIGH** | Project description rendered as raw text with `{project.description}` - safe for now since React escapes by default, but if this ever uses `dangerouslySetInnerHTML` or renders markdown, it needs sanitization | Add explicit sanitization if rich text is planned; document that plain text is safe |

### Bug Fixes Needed

| File:Line | Severity | Issue | Fix |
|-----------|----------|-------|-----|
| `ProjectDetail.tsx:46-52` | **MEDIUM** | Task fetch failures silently swallowed (`console.warn` only) | Set partial error state: `setTasksError('Could not load tasks')` with user-visible banner |
| `ProjectDetail.tsx:192` | **MEDIUM** | `project?.created_at || Date.now()` shows misleading current date when created_at is null | Use placeholder text: `project?.created_at ? new Date(project.created_at).toLocaleDateString() : 'Unknown'` |
| `AgentDetail.tsx:82-83` | **HIGH** | `t.executor === id` doesn't guard against null `t.id` | Filter should be: `t.executor === id && t.id != null` |
| `AgentDetail.tsx:114-116` | **HIGH** | Hardcoded fallback capabilities diverge from real agent capabilities | Remove fallback or fetch from a config endpoint |
| `Dashboard.tsx:131-133` | **MEDIUM** | API errors only logged to console | Add user-facing error state similar to line 204 banner |
| `ChatPanel.tsx:221-235` | **MEDIUM** | Model switch error only reverts local state + console.error, no toast | Add `showError('Failed to switch model')` in catch block |

### Duplicated Patterns

| Pattern | Occurrences | Files |
|---------|-------------|-------|
| Stat Card with icon (`rounded-xl border border-gray-800/80 bg-gray-900/60 p-4 shadow-lg backdrop-blur-sm flex items-center justify-between`) | 15+ | `AgentDetail.tsx`, `ProjectDetail.tsx`, `Projects.tsx`, `Dashboard.tsx`, `AgentStats.tsx` |
| Error Alert Banner (`p-4 rounded-xl bg-amber-500/10 border border-amber-500/30`) | 6 | All page components |
| Status Badge (`px-2.5 py-0.5 rounded-full border text-xs font-medium`) | 8+ | `ProjectDetail.tsx`, `TaskTable.tsx`, `AgentDetail.tsx` |
| Search Input with icon (`bg-gray-950/80 border border-gray-800 rounded-lg pl-9`) | 5 | `AgentDetail.tsx`, `Agents.tsx`, `Projects.tsx`, `ProjectDetail.tsx` |
| Filter Pills row (`px-3 py-1.5 rounded-lg text-xs font-medium`) | 4 | `Agents.tsx`, `Projects.tsx`, `ProjectDetail.tsx` |
| Modal wrapper (`fixed inset-0 z-50 bg-black/70 backdrop-blur-sm`) | 3 | `Agents.tsx`, `Projects.tsx`, `ProjectSettingsModal.tsx` |
| Page header with gradient icon + title | 5 | All pages |
| Loading skeleton placeholders | 4 | `Dashboard.tsx`, `Agents.tsx`, `Projects.tsx`, `AgentDetail.tsx` |

### Inconsistent Error Handling

- `ProjectDetail.tsx:46-52`: Silent `console.warn`
- `Dashboard.tsx:91-98`: Silent `.catch()` fallback to null
- `Agents.tsx:48-49`: Silent `console.warn`
- `ChatPanel.tsx:198-209`: Falls back to welcome message with no error indicator

**Recommendation:** Standardize with a `useDataFetch` hook pattern that exposes `{ data, loading, error, refetch }` and renders the error banner consistently.

---

## Component Reuse Recommendation

### Option A: Extract Internal `components/ui/` Primitives

**Pros:**
- Zero migration cost for existing code (incremental extraction)
- Exact match to existing Tailwind classes (no style drift)
- No bundle size increase from unused library components
- Full control over animation, hover states, dark mode

**Cons:**
- Maintenance burden on team (accessibility, keyboard nav)
- No built-in headless behavior for complex widgets (modals, dropdowns, popovers)
- Takes ~2-3 sprints to extract and test core set

**Estimated components to extract:**
1. `StatCard` - icon, label, value, trend
2. `AlertBanner` - severity (error/warning/info), message, retry action
3. `StatusBadge` - status prop → color mapping
4. `SearchInput` - icon, placeholder, value, onChange
5. `FilterPills` - items array, activeItem, onSelect
6. `Modal` - title, children, onClose
7. `PageHeader` - icon, title, subtitle, actions slot
8. `DataTable` - columns, data, sortable, paginated
9. `Skeleton` - variant (card, row, text)

### Option B: Adopt shadcn/ui (Recommended)

**Why shadcn/ui specifically:**
- Copy-paste model means components live in your repo (no runtime dependency)
- Built on Radix UI primitives (accessible, headless, battle-tested)
- Tailwind-native styling - works with existing `tailwind.config.js`
- Dark mode support built-in
- Components can be customized after paste

**Migration cost:**
- ~1 day to install and configure (CLI scaffolds `components/ui/`)
- ~2 days to migrate stat cards, badges, modals
- ~3 days to migrate tables (DataTable component)
- Total: ~1 sprint for core migration

**Bundle size:**
- Radix primitives add ~15-20 KB gzipped
- Only import what you use (tree-shakeable)

**Recommendation:** **Adopt shadcn/ui**

The existing codebase already uses Tailwind extensively and has no headless UI library. shadcn/ui's copy-paste model avoids lock-in while providing accessible primitives. The 15+ duplicated stat card patterns and 6+ error banners justify the migration cost.

### Tradeoffs Summary

| Factor | Extract Internal | shadcn/ui |
|--------|-----------------|-----------|
| Migration cost | ~3 sprints | ~1 sprint |
| Maintenance | High (DIY a11y) | Low (Radix handles it) |
| Bundle size | +0 KB | +15-20 KB |
| Tailwind compat | Perfect | Perfect |
| Future widgets (dropdowns, popovers, date pickers) | DIY or add library later | Already available |

---

## Phased Refactor Plan

### Phase 1: Foundation & Quick Wins (Weeks 1-2)

#### CTV2-119: Install shadcn/ui and Extract Core Primitives
**Files (5):**
- `frontend/package.json`
- `frontend/tailwind.config.js`
- `frontend/src/components/ui/stat-card.tsx` (new)
- `frontend/src/components/ui/alert-banner.tsx` (new)
- `frontend/src/components/ui/status-badge.tsx` (new)

**Test:** `frontend/src/components/ui/__tests__/stat-card.test.tsx`

#### CTV2-120: Fix High-Severity Bugs from Pre-scan
**Files (3):**
- `frontend/src/pages/AgentDetail.tsx` (lines 82-83, 114-116)
- `frontend/src/pages/ProjectDetail.tsx` (lines 46-52, 192)
- `frontend/src/pages/Dashboard.tsx` (line 133)

**Test:** `frontend/src/pages/__tests__/AgentDetail.test.tsx`

### Phase 2: Performance Optimization (Week 3)

#### CTV2-121: Add useMemo to Filtered Lists
**Files (5):**
- `frontend/src/pages/ProjectDetail.tsx`
- `frontend/src/pages/AgentDetail.tsx`
- `frontend/src/pages/Agents.tsx`
- `frontend/src/pages/Projects.tsx`
- `frontend/src/components/tasks/TaskTable.tsx`

**Test:** `frontend/src/pages/__tests__/ProjectDetail.test.tsx`

#### CTV2-122: Parallelize API Calls (Promise.all)
**Files (4):**
- `frontend/src/pages/ProjectDetail.tsx` (fetchProjectDetail)
- `frontend/src/pages/AgentDetail.tsx` (fetchAgentDetail)
- `frontend/src/pages/Dashboard.tsx` (fetchStatsOverview)
- `frontend/src/hooks/useDashboardStats.ts` (new - extract hook)

**Test:** `frontend/src/pages/__tests__/Dashboard.test.tsx`

### Phase 3: Component Splitting (Weeks 4-5)

#### CTV2-123: Split ChatPanel into Sub-components
**Files (6):**
- `frontend/src/components/chat/ChatPanel.tsx`
- `frontend/src/components/chat/ChatHeader.tsx` (new)
- `frontend/src/components/chat/MessageList.tsx` (new)
- `frontend/src/hooks/useSSEStream.ts` (new)
- `frontend/src/components/chat/__tests__/ChatPanel.test.tsx` (new)
- `frontend/src/components/chat/__tests__/MessageList.test.tsx` (new)

#### CTV2-124: Split AgentDetail Page
**Files (5):**
- `frontend/src/pages/AgentDetail.tsx`
- `frontend/src/components/agents/AgentProfileHeader.tsx` (new)
- `frontend/src/components/agents/AgentKpiCards.tsx` (new)
- `frontend/src/components/agents/AgentTaskList.tsx` (new)
- `frontend/src/pages/__tests__/AgentDetail.test.tsx` (new)

#### CTV2-125: Split ProjectDetail Page
**Files (5):**
- `frontend/src/pages/ProjectDetail.tsx`
- `frontend/src/components/projects/ProjectKpiCards.tsx` (new)
- `frontend/src/components/projects/ProjectTaskList.tsx` (new)
- `frontend/src/components/projects/ProjectHeader.tsx` (new)
- `frontend/src/pages/__tests__/ProjectDetail.test.tsx` (new)

### Phase 4: Shared UI Migration (Week 6)

#### CTV2-126: Migrate Stat Cards to Shared Component
**Files (7):**
- `frontend/src/components/ui/stat-card.tsx`
- `frontend/src/pages/Dashboard.tsx`
- `frontend/src/pages/AgentDetail.tsx`
- `frontend/src/pages/ProjectDetail.tsx`
- `frontend/src/pages/Projects.tsx`
- `frontend/src/components/agents/AgentStats.tsx`
- `frontend/src/components/dashboard/KpiCards.tsx`

#### CTV2-127: Migrate Alert Banners and Modals
**Files (8):**
- `frontend/src/components/ui/alert-banner.tsx`
- `frontend/src/components/ui/modal.tsx` (new, wrap Radix Dialog)
- `frontend/src/pages/Dashboard.tsx`
- `frontend/src/pages/Agents.tsx`
- `frontend/src/pages/Projects.tsx`
- `frontend/src/pages/AgentDetail.tsx`
- `frontend/src/pages/ProjectDetail.tsx`
- `frontend/src/components/projects/ProjectSettingsModal.tsx`

### Phase 5: Test Coverage for Untested Hotspots (Week 7)

#### CTV2-128: Add Tests for Dashboard and Projects Pages
**Files (4):**
- `frontend/src/pages/__tests__/Dashboard.test.tsx` (new)
- `frontend/src/pages/__tests__/Projects.test.tsx` (new)
- `frontend/src/pages/__tests__/ProjectDetail.test.tsx` (new)
- `frontend/src/test/mocks/handlers.ts` (add MSW handlers)

#### CTV2-129: Add Tests for Agents and ChatPanel
**Files (4):**
- `frontend/src/pages/__tests__/Agents.test.tsx` (new)
- `frontend/src/pages/__tests__/AgentDetail.test.tsx` (new)
- `frontend/src/components/chat/__tests__/ChatPanel.test.tsx` (new)
- `frontend/src/test/setup.ts` (ensure test config)

---

## Test Coverage Recommendations

The following components have **zero test coverage** per `get_knowledge_gaps_tool`:

| Component | Recommended Test File | Priority |
|-----------|----------------------|----------|
| `Dashboard` | `frontend/src/pages/__tests__/Dashboard.test.tsx` | High |
| `AgentsPage` | `frontend/src/pages/__tests__/Agents.test.tsx` | High |
| `AgentDetailPage` | `frontend/src/pages/__tests__/AgentDetail.test.tsx` | High |
| `ProjectsPage` | `frontend/src/pages/__tests__/Projects.test.tsx` | Medium |
| `ProjectDetailPage` | `frontend/src/pages/__tests__/ProjectDetail.test.tsx` | Medium |
| `ChatPanel` | `frontend/src/components/chat/__tests__/ChatPanel.test.tsx` | High (bridge node) |

**Testing strategy:**
1. Use MSW (Mock Service Worker) to mock API responses
2. Use React Testing Library for component tests
3. Focus on user interactions and API integration
4. Avoid testing implementation details

**Test priorities:**
1. **ChatPanel** - Architectural bridge node, critical for user experience
2. **Dashboard** - Entry point, high visibility
3. **AgentsPage/AgentDetail** - High fan-out (75/67 degree)
4. **Projects pages** - Lower priority but still untested hotspots

---

## Summary

**Immediate actions (CTV2-119, CTV2-120):**
- Install shadcn/ui primitives
- Fix 4 high-severity bugs from pre-scan

**Core refactor (CTV2-121 through CTV2-127):**
- Add memoization to prevent unnecessary re-renders
- Parallelize sequential API calls
- Split 5 large components into focused sub-components
- Migrate 15+ duplicated patterns to shared components

**Quality gate (CTV2-128, CTV2-129):**
- Add test coverage for 6 untested hotspots
- Target 60%+ coverage for critical paths

**Total tasks:** 11 follow-up CTV2 tasks  
**Estimated effort:** 7 weeks  
**Files impacted per task:** ≤8 (satisfies blast-radius constraint)
