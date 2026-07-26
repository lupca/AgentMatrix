"""Agent dispatch and run-control API."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Agent, AgentRun, Task
from app.services.task_orchestration import (
    ModeViolationError,
    OrchestrationError,
    PrerequisiteError,
    TaskNotFoundError,
    TaskOrchestrationService,
    TransitionConflictError,
    TransitionResult,
)
from app.workers.agent_runner import run_agent
from app.workers.output_streamer import publish_status, request_cancel

router = APIRouter(prefix="/api", tags=["dispatch"])


class DispatchRequest(BaseModel):
    task_id: str
    agent_id: str
    timeout_seconds: int = Field(default=14_400, ge=1, le=14_400)
    actor: str = "api"
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))


class DispatchResponse(BaseModel):
    run_id: str | None = None
    task_id: str
    agent_id: str
    command: str | None = None
    status: str
    gate_record_id: int


class GateDecisionRequest(BaseModel):
    decision: str
    actor: str
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))


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

    service = TaskOrchestrationService(db)
    try:
        result = service.request_dispatch(
            task_id=req.task_id,
            agent_id=req.agent_id,
            actor=req.actor,
            idempotency_key=req.idempotency_key,
            timeout_seconds=req.timeout_seconds,
        )
    except OrchestrationError as exc:
        _raise_orchestration_http(exc)
    if result.agent_run is not None:
        _enqueue_dispatch(result, service)
    return _dispatch_response(result, req.agent_id)


@router.post("/gates/{gate_record_id}/decision", response_model=DispatchResponse)
def decide_gate(
    gate_record_id: int,
    req: GateDecisionRequest,
    db: Session = Depends(get_db),
) -> DispatchResponse:
    service = TaskOrchestrationService(db)
    try:
        result = service.decide_gate(
            gate_record_id=gate_record_id,
            decision=req.decision,
            actor=req.actor,
            idempotency_key=req.idempotency_key,
        )
    except OrchestrationError as exc:
        _raise_orchestration_http(exc)
    if result.agent_run is not None:
        _enqueue_dispatch(result, service)
    agent_id = (
        result.agent_run.agent_id
        if result.agent_run is not None
        else str((result.gate_record.input_payload or {}).get("agent_id", ""))
    )
    return _dispatch_response(result, agent_id)


def _dispatch_response(
    result: TransitionResult,
    agent_id: str,
) -> DispatchResponse:
    return DispatchResponse(
        run_id=result.agent_run.id if result.agent_run is not None else None,
        task_id=result.task.id,
        agent_id=agent_id,
        command=result.agent_run.command if result.agent_run is not None else None,
        status=(
            result.agent_run.status
            if result.agent_run is not None
            else result.gate_record.status
        ),
        gate_record_id=result.gate_record.id,
    )


def _enqueue_dispatch(
    result: TransitionResult,
    service: TaskOrchestrationService,
) -> None:
    run = result.agent_run
    context = result.context or {}
    if run is None:
        return
    try:
        message = run_agent.send(
            run.id,
            run.task_id,
            run.command,
            str(context["repo_root"]),
            run.timeout_seconds,
        )
        message_id = getattr(message, "message_id", None)
        if message_id:
            run.dramatiq_message_id = str(message_id)
            service.db.commit()
    except Exception as exc:
        error = f"Could not queue run: {exc}"
        service.record_dispatch_queue_failure(
            run_id=run.id,
            error=error,
            actor="system:dispatch-queue",
            idempotency_key=f"{result.gate_record.idempotency_key}:queue-failure",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error,
        ) from exc


def _raise_orchestration_http(exc: OrchestrationError) -> None:
    if isinstance(exc, TaskNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, TransitionConflictError):
        code = status.HTTP_409_CONFLICT
    elif isinstance(exc, ModeViolationError):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, PrerequisiteError):
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        code = status.HTTP_409_CONFLICT
    raise HTTPException(status_code=code, detail=str(exc)) from exc


@router.get("/dispatch/{run_id}", response_model=AgentRunResponse)
def get_run_status(run_id: str, db: Session = Depends(get_db)) -> AgentRun:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return run


@router.post("/dispatch/{run_id}/cancel")
@router.post("/runs/{run_id}/cancel")
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

    service = TaskOrchestrationService(db)
    try:
        service.cancel_run(
            run_id=run_id,
            actor="api",
            idempotency_key=f"cancel:{run_id}",
        )
    except OrchestrationError as exc:
        _raise_orchestration_http(exc)
    publish_status(run_id, "cancelled", error=run.error_message)
    return {"run_id": run_id, "status": "cancelled"}
