import re
import uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import (
    AgentRun as AgentRunModel,
    AuditLog as AuditLogModel,
    Session as SessionModel,
    Task as TaskModel,
)
from app.api.dispatch import AgentRunResponse, _enqueue_dispatch, _raise_orchestration_http
from app.graph.context import invalidate_context_snapshot
from app.schemas.task import Task, TaskCreate, TaskUpdate
from app.schemas.audit import AuditLog
from app.schemas.agent import AgentSuggestion
from app.services.agent_matcher import AgentMatcher
from app.services.task_orchestration import (
    OrchestrationError,
    TaskOrchestrationService,
)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class ReviewRequest(BaseModel):
    reviewer: str
    actor: str
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))


class VerdictRequest(BaseModel):
    verdict: str
    ac_results: Any
    actor: str
    findings: list[Any] = Field(default_factory=list)
    idempotency_key: str = Field(default_factory=lambda: str(uuid.uuid4()))


def generate_task_id(db: Session, project: str) -> str:
    clean_proj = project.strip()
    if "-" in clean_proj or "_" in clean_proj:
        parts = re.split(r"[-_]+", clean_proj)
        prefix = "".join([p[0].upper() for p in parts if p])
    else:
        prefix = clean_proj.upper()

    if not prefix:
        prefix = "TASK"

    pattern = f"{prefix}-%"
    existing_tasks = db.query(TaskModel.id).filter(TaskModel.id.like(pattern)).all()

    max_seq = 0
    for (t_id,) in existing_tasks:
        try:
            seq_str = t_id.split("-")[-1]
            seq = int(seq_str)
            if seq > max_seq:
                max_seq = seq
        except (ValueError, IndexError):
            continue

    return f"{prefix}-{max_seq + 1:03d}"


@router.get("", response_model=list[Task])
def get_tasks(
    status: str | None = None,
    project: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(TaskModel)
    if status:
        query = query.filter(TaskModel.status == status)
    if project:
        query = query.filter(TaskModel.project == project)
    
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=Task, status_code=status.HTTP_201_CREATED)
def create_task(task_in: TaskCreate, db: Session = Depends(get_db)):
    task_data = task_in.model_dump(exclude_unset=True)
    depends_on = task_data.pop("depends_on", None) or []

    task_id = task_data.get("id")
    if not task_id:
        task_id = generate_task_id(db, task_in.project)
        task_data["id"] = task_id
    else:
        existing = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Task with ID '{task_id}' already exists."
            )

    if not task_data.get("session_id"):
        task_data["session_id"] = str(uuid.uuid4())

    db_task = TaskModel(**task_data)
    db.add(db_task)

    # Ensure Session entry exists
    db_session = SessionModel(
        id=db_task.session_id,
        task_id=db_task.id,
        project_id=db_task.project,
        context_level="task",
        thread_id=db_task.session_id,
        messages=[]
    )
    db.add(db_session)

    # Auto audit log entry
    audit_entry = AuditLogModel(
        task_id=task_id,
        action="create_task",
        actor="system",
        details={
            "title": task_in.title,
            "project": task_in.project,
            "status": db_task.status
        }
    )
    db.add(audit_entry)

    db.commit()
    db.refresh(db_task)

    if depends_on:
        service = TaskOrchestrationService(db)
        for dep_id in depends_on:
            try:
                service.add_dependency(
                    task_id=db_task.id, depends_on_task_id=dep_id, actor="system"
                )
            except OrchestrationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
        db.refresh(db_task)

    invalidate_context_snapshot(db, project_id=db_task.project)
    return db_task


@router.get("/{id}", response_model=Task)
def get_task(id: str, db: Session = Depends(get_db)):
    db_task = db.query(TaskModel).filter(TaskModel.id == id).first()
    if not db_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{id}' not found."
        )
    if not db_task.session_id:
        db_task.session_id = str(uuid.uuid4())
        db.commit()
        db.refresh(db_task)
    return db_task


@router.get("/{id}/suggested-agents", response_model=list[AgentSuggestion])
def get_suggested_agents(
    id: str,
    top_n: int = Query(3, ge=1, le=10),
    db: Session = Depends(get_db),
):
    db_task = db.query(TaskModel).filter(TaskModel.id == id).first()
    if not db_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{id}' not found.",
        )
    return AgentMatcher(db).suggest_agents(db_task, top_n=top_n)


@router.get("/{id}/runs", response_model=list[AgentRunResponse])
def get_task_runs(id: str, db: Session = Depends(get_db)):
    db_task = db.query(TaskModel).filter(TaskModel.id == id).first()
    if not db_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{id}' not found."
        )

    return (
        db.query(AgentRunModel)
        .filter(AgentRunModel.task_id == id)
        .order_by(AgentRunModel.queued_at.desc())
        .all()
    )


@router.post("/{id}/review")
def request_task_review(
    id: str,
    request: ReviewRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = TaskOrchestrationService(db)
    try:
        result = service.request_review(
            task_id=id,
            reviewer=request.reviewer,
            actor=request.actor,
            idempotency_key=request.idempotency_key,
        )
    except OrchestrationError as exc:
        _raise_orchestration_http(exc)
    if result.agent_run is not None:
        _enqueue_dispatch(result, service)
    return {
        "task_id": id,
        "status": result.task.status,
        "decision_status": result.status,
        "gate_record_id": result.gate_record.id,
        "applied": result.applied,
    }


@router.post("/{id}/verdict")
def request_task_verdict(
    id: str,
    request: VerdictRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = TaskOrchestrationService(db).request_verdict(
            task_id=id,
            verdict=request.verdict,
            ac_results=request.ac_results,
            actor=request.actor,
            findings=request.findings,
            idempotency_key=request.idempotency_key,
        )
    except OrchestrationError as exc:
        _raise_orchestration_http(exc)
    return {
        "task_id": id,
        "status": result.task.status,
        "decision_status": result.status,
        "gate_record_id": result.gate_record.id,
        "applied": result.applied,
    }


@router.patch("/{id}", response_model=Task)
def update_task(id: str, task_in: TaskUpdate, db: Session = Depends(get_db)):
    db_task = db.query(TaskModel).filter(TaskModel.id == id).first()
    if not db_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{id}' not found."
        )

    update_data = task_in.model_dump(exclude_unset=True)
    if not update_data:
        return db_task

    for field, value in update_data.items():
        setattr(db_task, field, value)

    # Auto audit log entry
    actor = db_task.executor or "system"
    audit_entry = AuditLogModel(
        task_id=id,
        action="update_task",
        actor=actor,
        details=update_data
    )
    db.add(audit_entry)

    db.commit()
    db.refresh(db_task)
    invalidate_context_snapshot(db, project_id=db_task.project)
    return db_task


@router.get("/{id}/history", response_model=list[AuditLog])
def get_task_history(id: str, db: Session = Depends(get_db)):
    db_task = db.query(TaskModel).filter(TaskModel.id == id).first()
    if not db_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{id}' not found."
        )
    history = db.query(AuditLogModel).filter(AuditLogModel.task_id == id).order_by(AuditLogModel.created_at.asc()).all()
    return history


@router.get("/{id}/messages")
def get_task_messages(id: str, db: Session = Depends(get_db)):
    db_task = db.query(TaskModel).filter(TaskModel.id == id).first()
    if not db_task:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{id}' not found."
        )

    db_session = db.query(SessionModel).filter(
        (SessionModel.task_id == id) | (SessionModel.id == db_task.session_id)
    ).first()

    if not db_session:
        if not db_task.session_id:
            db_task.session_id = str(uuid.uuid4())
            db.commit()
            db.refresh(db_task)

        db_session = SessionModel(
            id=db_task.session_id,
            task_id=db_task.id,
            project_id=db_task.project,
            context_level="task",
            thread_id=db_task.session_id,
            messages=[]
        )
        db.add(db_session)
        db.commit()
        db.refresh(db_session)

    return db_session.messages or []
