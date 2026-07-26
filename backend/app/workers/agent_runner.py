import dramatiq
import subprocess
import redis
from app.workers import redis_broker

r = redis.Redis.from_url('redis://redis:6379/0')

@dramatiq.actor(max_retries=3, min_backoff=30000)
def run_agent(run_id: str, task_id: str, command: str, repo_root: str):
    r.publish(f'run:{run_id}', 'started')
    result = subprocess.run(command, shell=True, cwd=repo_root, capture_output=True, text=True)
    r.publish(f'run:{run_id}', f'done:{result.returncode}')
    return result.returncode
