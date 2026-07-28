from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db.base import Base, engine
from app.api import (
    agents,
    audit,
    chat,
    dispatch,
    events,
    knowledge,
    projects,
    sessions,
    stats,
    stream,
    tasks,
    ws,
)

# Ensure tables are created
Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import os
    if not os.getenv("TESTING"):
        ws.start_redis_subscriber()
    yield
    if not os.getenv("TESTING"):
        ws.stop_redis_subscriber()


app = FastAPI(
    title="Control Tower V2 API",
    description="FastAPI CRUD & State Management for Control Tower V2",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["health"])
def health_check():
    return {"status": "ok"}


app.include_router(tasks.router)
app.include_router(projects.router)
app.include_router(agents.router)
app.include_router(sessions.router)
app.include_router(audit.router)
app.include_router(chat.router)
app.include_router(ws.router)
app.include_router(knowledge.router)
app.include_router(stats.router)
app.include_router(dispatch.router)
app.include_router(stream.router)
app.include_router(events.router)

