from app.db.models import Agent, Task


class AgentSelector:
    def __init__(self, db):
        self.db = db

    def select(self, task: Task, agents: list[Agent]) -> Agent:
        # Score by success_rate
        scored = [(a.success_rate or 1.0, a) for a in agents]
        scored.sort(reverse=True)
        return scored[0][1] if scored else None
