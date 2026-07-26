import os
import subprocess
import dramatiq
import redis
from app.workers import redis_broker

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
r = redis.Redis.from_url(REDIS_URL)


@dramatiq.actor(max_retries=3, min_backoff=30000)
def run_agent(run_id: str, task_id: str, command: str, repo_root: str):
    r.publish(f"run:{run_id}", "started")
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        r.publish(f"run:{run_id}", f"done:{result.returncode}")
        return result.returncode
    except Exception as e:
        r.publish(f"run:{run_id}", f"error:{str(e)}")
        raise
