import os
import subprocess
import dramatiq
import redis
from app.workers import redis_broker

REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
r = redis.Redis.from_url(REDIS_URL)


@dramatiq.actor(max_retries=3, min_backoff=30000, time_limit=14400000)
def run_agent(run_id: str, task_id: str, command: str, repo_root: str):
    channel = f'run:{run_id}'
    r.publish(channel, 'started')
    
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1
    )
    
    for line in process.stdout:
        r.publish(channel, line.rstrip())
    
    exit_code = process.wait()
    r.publish(channel, f'__DONE__{exit_code}')
    return exit_code
