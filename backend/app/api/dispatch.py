from fastapi import APIRouter
router = APIRouter(prefix='/api', tags=['dispatch'])

@router.post('/dispatch')
def dispatch_task(task_id: str, agent_id: str, command: str):
    from app.workers.agent_runner import run_agent
    run_agent.send(task_id, task_id, command, '/tmp')
    return {'status': 'queued'}
