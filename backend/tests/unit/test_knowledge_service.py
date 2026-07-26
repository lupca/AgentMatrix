import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from app.db.models import KnowledgeItem
from app.services.knowledge_service import KnowledgeService


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_knowledge_service_get_relevant(db_session):
    k1 = KnowledgeItem(id="k-1", title="Item 1", project="proj-a")
    k2 = KnowledgeItem(id="k-2", title="Item 2", project="proj-b")
    db_session.add_all([k1, k2])
    db_session.commit()

    service = KnowledgeService(db_session)
    items = service.get_relevant(project="proj-a", gate="spec")

    assert len(items) == 1
    assert items[0].id == "k-1"
