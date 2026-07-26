from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.db.base import Base, engine
from app.api import tasks, sessions, audit, chat, ws, projects, agents, knowledge, stats

# Ensure tables are created
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Control Tower V2 API",
    description="FastAPI CRUD & State Management for Control Tower V2",
    version="0.1.0"
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
