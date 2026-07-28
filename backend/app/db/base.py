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

engine_kwargs = {"pool_pre_ping": True}
if "sqlite" not in DATABASE_URL:
    engine_kwargs.update({"pool_size": 5, "max_overflow": 10})

engine = create_engine(
    DATABASE_URL,
    **engine_kwargs
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
