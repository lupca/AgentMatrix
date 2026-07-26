import re
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Task as TaskModel, Session as SessionModel, AuditLog as AuditLogModel
from app.schemas.task import Task, TaskCreate, TaskUpdate
from app.schemas.audit import AuditLog

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


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
        thread_id=db_task.session_id,
        messages=[]
    )
    db.add(db_session)

    # Auto audit log entry
    audit_entry = AuditLogModel(
        task_id=task_id,
        action="create_task",
        actor=task_in.executor or "system",
        details={
            "title": task_in.title,
            "project": task_in.project,
            "status": db_task.status
        }
    )
    db.add(audit_entry)
    
    db.commit()
    db.refresh(db_task)
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
    actor = update_data.get("executor") or update_data.get("reviewer") or db_task.executor or "system"
    audit_entry = AuditLogModel(
        task_id=id,
        action="update_task",
        actor=actor,
        details=update_data
    )
    db.add(audit_entry)

    db.commit()
    db.refresh(db_task)
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
            thread_id=db_task.session_id,
            messages=[]
        )
        db.add(db_session)
        db.commit()
        db.refresh(db_session)

    return db_session.messages or []
