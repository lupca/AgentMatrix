import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.db.base import Base
from app.db.base import get_db
from fastapi.testclient import TestClient
from app.main import app

TEST_DB = 'postgresql://ct:secret@localhost:5433/control_tower_test'


@pytest.fixture
def git_repo_root(tmp_path):
    """A real git repo with one commit, for tests exercising base/head tracking."""
    path = tmp_path / "repo"
    path.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731
        args, cwd=path, check=True, capture_output=True, text=True
    )
    run("git", "init", "-q")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "README.md").write_text("init\n")
    run("git", "add", ".")
    run("git", "commit", "-q", "-m", "init")
    return str(path)


def commit_change(repo_root: str, message: str = "change") -> None:
    """Simulate an executor landing a real commit inside repo_root."""
    from pathlib import Path

    (Path(repo_root) / f"{message}.txt").write_text(message)
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_root, check=True)

@pytest.fixture(scope='session')
def test_db():
    engine = create_engine(TEST_DB)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)

@pytest.fixture
def db_session():
    """Give every test an isolated, portable database."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def client(db_session):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
