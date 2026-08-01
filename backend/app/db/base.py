import os
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker, DeclarativeBase, Session

# In Docker: DATABASE_URL is set by docker-compose
# Local dev: set DATABASE_URL env var or use default
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://ct:secret@localhost:5433/control_tower"
)

DATABASE_URL_READONLY = os.getenv("DATABASE_URL_READONLY")

engine_kwargs = {"pool_pre_ping": True}
if "sqlite" not in DATABASE_URL:
    engine_kwargs.update({"pool_size": 5, "max_overflow": 10})

engine = create_engine(
    DATABASE_URL,
    **engine_kwargs
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Read-only setup
SessionLocalReadonly = None
if DATABASE_URL_READONLY:
    readonly_kwargs = {"pool_pre_ping": True}
    if "sqlite" not in DATABASE_URL_READONLY:
        readonly_kwargs.update({"pool_size": 3, "max_overflow": 5})
    engine_readonly = create_engine(DATABASE_URL_READONLY, **readonly_kwargs)
    SessionLocalReadonly = sessionmaker(autocommit=False, autoflush=False, bind=engine_readonly)

class Base(DeclarativeBase):
    pass

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_readonly_db() -> Generator[Session, None, None]:
    if not SessionLocalReadonly:
        raise RuntimeError("DATABASE_URL_READONLY is not configured. Please run scripts/create-readonly-role.sh and set the environment variable.")
    db = SessionLocalReadonly()
    try:
        yield db
    finally:
        db.close()
