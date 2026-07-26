"""Agent dispatch and run-control API."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Agent, AgentRun, Project, Task
from app.services.command_builder import build_dispatch_command
from app.workers.agent_runner import run_agent
from app.workers.output_streamer import publish_status, request_cancel

router = APIRouter(prefix="/api", tags=["dispatch"])


class DispatchRequest(BaseModel):
    task_id: str
    agent_id: str
    timeout_seconds: int = Field(default=14_400, ge=1, le=14_400)


class DispatchResponse(BaseModel):
    run_id: str
    task_id: str
    agent_id: str
    command: str
    status: str


class AgentRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    agent_id: str
    cli: str
    command: str
    status: str
    pid: int | None
    dramatiq_message_id: str | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    timeout_seconds: int
    exit_code: int | None
    result_ref: str | None
    error_message: str | None
    output_lines: int
    output_bytes: int
    attempt: int
    max_attempts: int


@router.post("/dispatch", response_model=DispatchResponse)
def dispatch_agent(req: DispatchRequest, db: Session = Depends(get_db)) -> DispatchResponse:
    task = db.query(Task).filter(Task.id == req.task_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {req.task_id} not found")

    agent = db.query(Agent).filter(Agent.id == req.agent_id).first()
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {req.agent_id} not found")

    active_run = (
        db.query(AgentRun)
        .filter(
            AgentRun.task_id == req.task_id,
            AgentRun.status.in_(["queued", "running"]),
        )
        .first()
    )
    if active_run is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Task {req.task_id} already has active run: {active_run.id}",
        )

    project = db.query(Project).filter(Project.id == task.project).first()
    try:
        command, repo_root, cli = build_dispatch_command(task, agent, project)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    run = AgentRun(
        task_id=req.task_id,
        agent_id=req.agent_id,
        cli=cli,
        command=command,
        status="queued",
        timeout_seconds=req.timeout_seconds,
    )
    task.status = "dispatched"
    task.executor = req.agent_id
    task.dispatched_at = datetime.now(timezone.utc)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        message = run_agent.send(
            run.id,
            req.task_id,
            command,
            repo_root,
            req.timeout_seconds,
        )
        message_id = getattr(message, "message_id", None)
        if message_id:
            run.dramatiq_message_id = str(message_id)
            db.commit()
    except Exception as exc:
        run.status = "failed"
        run.error_message = f"Could not queue run: {exc}"
        run.completed_at = datetime.now(timezone.utc)
        task.status = "todo"
        task.error = run.error_message
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=run.error_message,
        ) from exc

    return DispatchResponse(
        run_id=run.id,
        task_id=req.task_id,
        agent_id=req.agent_id,
        command=command,
        status="queued",
    )


@router.get("/dispatch/{run_id}", response_model=AgentRunResponse)
def get_run_status(run_id: str, db: Session = Depends(get_db)) -> AgentRun:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run


@router.post("/dispatch/{run_id}/cancel")
def cancel_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status not in {"queued", "running"}:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel run in status: {run.status}",
        )

    try:
        request_cancel(run_id, ttl_seconds=max(run.timeout_seconds + 300, 3_600))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not signal cancellation: {exc}",
        ) from exc

    run.status = "cancelled"
    run.error_message = "Cancelled by user"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()
    publish_status(run_id, "cancelled", error=run.error_message)
    return {"run_id": run_id, "status": "cancelled"}
