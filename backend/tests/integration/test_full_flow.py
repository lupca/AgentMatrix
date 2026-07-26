import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.db.models import Agent, AgentOutputChunk, AgentRun, Project, Task
from app.services.task_orchestration import TaskOrchestrationService


def _sse_events(body: str) -> list[dict]:
    return [json.loads(line.removeprefix('data: ')) for line in body.splitlines() if line.startswith('data: ')]


def _run_command(client, thread_id: str, message: str) -> dict:
    response = client.post('/api/chat', json={'thread_id': thread_id, 'message': message})
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert events[-1]['type'] == 'done'
    return json.loads(events[-1]['content'])


def _wait_for_terminal_run(client, run_id: str, timeout_seconds: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = client.get(f'/api/dispatch/{run_id}')
        assert response.status_code == 200, response.text
        run = response.json()
        if run['status'] in {'success', 'failed', 'timeout', 'cancelled'}:
            return run
        time.sleep(0.01)
    raise AssertionError(f'Run {run_id} did not reach a terminal state')


def test_create_dispatch_stream_and_complete_flow(client, db_session):
    """Exercise /pm -> dispatch -> SSE replay -> verdict as one API flow."""
    project_id = 'test-e2e-full-flow'
    agent_id = '@test-agent'
    thread_id = f'e2e-thread-{uuid.uuid4()}'
    repo_root = str(Path(__file__).resolve().parents[3])
    db_session.add(Project(id=project_id, name='E2E Full Flow', repo_root=repo_root))
    db_session.add(Agent(id=agent_id, name='Test Agent', role='executor', capabilities=['testing'], cli='codex', status='idle'))
    db_session.commit()

    created = _run_command(client, thread_id, f'/pm Complete API task flow --project {project_id}')
    assert created['action'] == 'created'
    task_id = created['task_id']
    assert task_id.startswith('TEST-')
    task = db_session.get(Task, task_id)
    assert task is not None and task.status == 'todo'

    # Keep the broker boundary real while simulating the durable worker result;
    # this keeps the integration test independent of Redis and Dramatiq.
    pending_dispatch = client.post(
        '/api/dispatch',
        json={
            'task_id': task_id,
            'agent_id': agent_id,
            'timeout_seconds': 60,
            'actor': '@operator',
            'idempotency_key': 'e2e-dispatch',
        },
    )
    assert pending_dispatch.status_code == 200, pending_dispatch.text
    assert pending_dispatch.json()['status'] == 'pending'
    with patch('app.api.dispatch.run_agent') as actor:
        actor.send.return_value = MagicMock(message_id='e2e-message-001')
        dispatched = client.post(
            f"/api/gates/{pending_dispatch.json()['gate_record_id']}/decision",
            json={
                'decision': 'approved',
                'actor': '@supervisor',
                'idempotency_key': 'e2e-dispatch-approval',
            },
        )
    dispatch_data = dispatched.json()
    run_id = dispatch_data['run_id']
    assert dispatch_data['status'] == 'queued' and dispatch_data['agent_id'] == agent_id
    actor.send.assert_called_once()

    db_session.expire_all()
    run = db_session.get(AgentRun, run_id)
    task = db_session.get(Task, task_id)
    assert run is not None and run.status == 'queued'
    assert task is not None and task.status == 'dispatched'
    run.status = 'success'
    run.exit_code = 0
    run.completed_at = datetime.now(timezone.utc)
    run.output_lines = 2
    run.output_bytes = len('agent started\nagent completed'.encode('utf-8'))
    db_session.add(AgentOutputChunk(run_id=run_id, chunk_index=0, content='agent started\nagent completed'))
    TaskOrchestrationService(db_session).record_execution_success(
        task_id=task_id,
        result_ref='result-sha',
        actor=agent_id,
        idempotency_key='e2e-execution-success',
        run_id=run_id,
    )

    terminal_run = _wait_for_terminal_run(client, run_id)
    assert terminal_run['status'] == 'success' and terminal_run['exit_code'] == 0
    stream = client.get(f'/api/runs/{run_id}/stream')
    assert stream.status_code == 200
    events = _sse_events(stream.text)
    assert [event['content'] for event in events if event['type'] == 'history'] == ['agent started', 'agent completed']
    assert events[-2]['type'] == 'status' and events[-2]['status'] == 'success'
    assert events[-1]['type'] == 'done'

    review = client.post(
        f'/api/tasks/{task_id}/review',
        json={
            'reviewer': '@reviewer',
            'actor': '@operator',
            'idempotency_key': 'e2e-review',
        },
    )
    assert review.json()['decision_status'] == 'pending'
    approved_review = client.post(
        f"/api/gates/{review.json()['gate_record_id']}/decision",
        json={
            'decision': 'approved',
            'actor': '@supervisor',
            'idempotency_key': 'e2e-review-approval',
        },
    )
    assert approved_review.status_code == 200

    verdict = _run_command(
        client,
        thread_id,
        f'/verdict {task_id} pass [{{"passed": true}}]',
    )
    assert verdict['decision_status'] == 'pending'
    approved_verdict = client.post(
        f"/api/gates/{verdict['gate_record_id']}/decision",
        json={
            'decision': 'approved',
            'actor': '@supervisor',
            'idempotency_key': 'e2e-verdict-approval',
        },
    )
    assert approved_verdict.status_code == 200
    final_task = client.get(f'/api/tasks/{task_id}')
    assert final_task.status_code == 200
    assert final_task.json()['status'] == 'done'
