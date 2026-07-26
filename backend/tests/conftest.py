import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.base import Base
from fastapi.testclient import TestClient
from app.main import app

TEST_DB = 'postgresql://ct:secret@localhost:5433/control_tower_test'

@pytest.fixture(scope='session')
def test_db():
    engine = create_engine(TEST_DB)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
