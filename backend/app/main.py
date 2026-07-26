from fastapi import FastAPI
from app.db.base import Base, engine
from app.api import tasks, sessions, audit

# Ensure tables are created
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Control Tower V2 API",
    description="FastAPI CRUD & State Management for Control Tower V2",
    version="0.1.0"
)

@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}

app.include_router(tasks.router)
app.include_router(sessions.router)
app.include_router(audit.router)
