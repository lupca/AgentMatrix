import factory
from app.db.models import Task, Project, Agent

class TaskFactory(factory.Factory):
    class Meta:
        model = Task
    id = factory.Sequence(lambda n: f'T-{n:03d}')
    project = 'test'
    title = factory.Faker('sentence')
    status = 'todo'
