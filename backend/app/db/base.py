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

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True
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
