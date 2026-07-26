import { test, expect } from '@playwright/test'

type MockTask = {
  id: string; session_id: string; project: string; title: string; raw_input: string
  status: string; current_gate: string; mode: string; executor: string | null; reviewer: string | null
  acceptance_criteria: string[]; files: string[]; tests: string[]; flows: string[]
  plan: string | null; verdict: string | null; awaiting_approval: boolean; error: string | null
  created_at: string; updated_at: string
}

const BOOTSTRAP_TASK_ID = 'E2E-BOOT-001'
const TASK_ID = 'E2E-001'
const RUN_ID = 'e2e-run-001'
const AGENT_ID = '@e2e-agent'

function makeTask(overrides: Partial<MockTask> = {}): MockTask {
  const timestamp = '2026-01-01T00:00:00.000Z'
  return {
    id: TASK_ID, session_id: 'session-e2e-001', project: 'e2e-project', title: 'Complete browser task flow',
    raw_input: 'Create and complete a task from the browser.', status: 'todo', current_gate: 'spec', mode: 'supervised',
    executor: null, reviewer: null, acceptance_criteria: ['The task reaches done after a passing verdict.'],
    files: [], tests: [], flows: [], plan: null, verdict: null, awaiting_approval: false, error: null,
    created_at: timestamp, updated_at: timestamp, ...overrides,
  }
}

function jsonResponse(body: unknown, status = 200) {
  return { status, contentType: 'application/json', body: JSON.stringify(body) }
}

function chatResponse(content: Record<string, unknown>) {
  const encoded = JSON.stringify(content)
  return {
    status: 200, contentType: 'text/event-stream',
    body: [
      'data: {"type":"start","id":"e2e-chat-message"}',
      `data: {"type":"chunk","content":${JSON.stringify(encoded)}}`,
      `data: {"type":"done","id":"e2e-chat-message","content":${JSON.stringify(encoded)}}`, '',
    ].join('\n\n'),
  }
}

test('complete task flow: create, dispatch, stream output, and complete', async ({ page }) => {
  let targetTask: MockTask | null = null
  let runCreated = false
  const bootstrapTask = makeTask({
    id: BOOTSTRAP_TASK_ID, session_id: 'session-bootstrap', title: 'E2E bootstrap task',
    raw_input: 'Bootstrap task used to access the task copilot.',
  })

  // Use the real React application and browser SSE client while keeping the
  // API deterministic for headless CI (no Postgres, Redis, or agent process).
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/tasks' && request.method() === 'GET') {
      return route.fulfill(jsonResponse([bootstrapTask, ...(targetTask ? [targetTask] : [])]))
    }
    if (path === '/api/agents' && request.method() === 'GET') {
      return route.fulfill(jsonResponse([{ id: AGENT_ID, name: 'E2E Agent', role: 'executor', capabilities: ['testing'], status: 'idle' }]))
    }
    if (path.startsWith('/api/sessions/') && request.method() === 'GET') {
      return route.fulfill(jsonResponse({ messages: [] }))
    }
    const taskMatch = path.match(/^\/api\/tasks\/([^/]+)$/)
    if (taskMatch && request.method() === 'GET') {
      const taskId = decodeURIComponent(taskMatch[1])
      const task = taskId === BOOTSTRAP_TASK_ID ? bootstrapTask : targetTask
      return task ? route.fulfill(jsonResponse(task)) : route.fulfill(jsonResponse({ detail: 'Task not found' }, 404))
    }
    if (path.match(/^\/api\/tasks\/([^/]+)\/suggested-agents$/) && request.method() === 'GET') {
      return route.fulfill(jsonResponse([{ agent_id: AGENT_ID, score: 1, reason: 'Deterministic E2E executor' }]))
    }
    if (path.match(/^\/api\/tasks\/([^/]+)\/runs$/) && request.method() === 'GET') {
      return route.fulfill(jsonResponse(runCreated ? [{
        id: RUN_ID, task_id: TASK_ID, agent_id: AGENT_ID, cli: 'codex', command: 'codex exec e2e task', status: 'success',
        queued_at: '2026-01-01T00:00:01.000Z', started_at: '2026-01-01T00:00:01.100Z', completed_at: '2026-01-01T00:00:02.000Z',
        timeout_seconds: 60, exit_code: 0, result_ref: null, error_message: null, output_lines: 1, output_bytes: 18, attempt: 1, max_attempts: 1,
      }] : []))
    }
    if (path === '/api/chat' && request.method() === 'POST') {
      const body = request.postDataJSON() as { message?: string }
      const message = body.message?.trim() || ''
      if (message.startsWith('/pm ')) {
        targetTask = makeTask()
        return route.fulfill(chatResponse({ action: 'created', task_id: TASK_ID, title: targetTask.title, project: targetTask.project }))
      }
      if (message === `/verdict ${TASK_ID} pass` && targetTask) {
        targetTask = makeTask({ ...targetTask, status: 'done', current_gate: 'verdict', verdict: 'pass' })
        return route.fulfill(chatResponse({ action: 'verdict', task_id: TASK_ID, verdict: 'pass', new_status: 'done' }))
      }
    }
    if (path === '/api/dispatch' && request.method() === 'POST') {
      runCreated = true
      if (targetTask) targetTask = makeTask({ ...targetTask, status: 'dispatched', current_gate: 'dispatch', executor: AGENT_ID })
      return route.fulfill(jsonResponse({ run_id: RUN_ID, task_id: TASK_ID, agent_id: AGENT_ID, command: 'codex exec e2e task', status: 'queued' }))
    }
    if (path === `/api/runs/${RUN_ID}/stream` && request.method() === 'GET') {
      return route.fulfill({
        status: 200, headers: { 'Cache-Control': 'no-cache', 'Content-Type': 'text/event-stream' },
        body: [
          'id: 1', 'event: history', 'data: {"type":"history","content":"agent completed the task","index":1}', '',
          'event: status', 'data: {"type":"status","status":"success"}', '', 'event: done', 'data: {"type":"done"}', '',
        ].join('\n'),
      })
    }
    return route.continue()
  })

  await page.goto('/tasks')
  await page.getByRole('link', { name: BOOTSTRAP_TASK_ID, exact: true }).click()
  const chatInput = page.getByPlaceholder(/Message Control Tower AI/)
  await expect(chatInput).toBeEnabled()
  await chatInput.fill('/pm Complete browser task flow --project e2e-project')
  await chatInput.press('Enter')
  await expect(page.getByText(new RegExp(TASK_ID))).toBeVisible()

  await page.goto(`/tasks/${TASK_ID}`)
  await expect(page.getByRole('heading', { name: 'Complete browser task flow' })).toBeVisible()
  const dispatchButton = page.getByRole('button', { name: 'Dispatch Task', exact: true })
  await expect(dispatchButton).toBeEnabled()
  await dispatchButton.click()
  await expect(page.locator('select:has(option[value="dispatched"])')).toHaveValue('dispatched')
  await page.getByRole('button', { name: 'View output', exact: true }).click()
  await expect(page.getByLabel('Agent output', { exact: true })).toContainText('agent completed the task')

  await chatInput.fill(`/verdict ${TASK_ID} pass`)
  await chatInput.press('Enter')
  await expect(page.getByText(/"new_status":"done"/)).toBeVisible()
  await page.reload()
  await expect(page.locator('select:has(option[value="done"])')).toHaveValue('done')
})
