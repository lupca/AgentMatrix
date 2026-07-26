from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import ContextLevel, Session as SessionModel, SessionStatus, Task as TaskModel
from app.schemas.session import Session as SessionSchema, SessionCreate, SessionUpdate
from app.services.coordinator import ProviderRouter

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _validate_model_selection(
    data: dict,
    db_session: SessionModel | None = None,
) -> dict:
    """Normalize and validate optional per-session coordinator selection."""

    model = data.get(
        "selected_model",
        db_session.selected_model if db_session is not None else None,
    )
    provider = data.get(
        "selected_provider",
        db_session.selected_provider if db_session is not None else None,
    )
    if provider is not None:
        provider = provider.lower()
        if provider not in {"anthropic", "google"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Unsupported coordinator provider '{provider}'.",
            )
        if "selected_provider" in data:
            data["selected_provider"] = provider
    if model is not None:
        try:
            inferred = ProviderRouter.provider_name(model)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        if provider is not None and provider != inferred:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Model '{model}' belongs to '{inferred}', not '{provider}'.",
            )
        data["selected_provider"] = inferred
    return data


def _resolve_context(session_data: dict, db: Session) -> dict:
    """Derive project_id from task_id where possible and enforce context-level consistency."""

    context_level = session_data.get("context_level", ContextLevel.GLOBAL)
    task_id = session_data.get("task_id")
    project_id = session_data.get("project_id")

    if task_id is not None:
        task = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task '{task_id}' not found.",
            )
        if project_id is None:
            project_id = task.project
            session_data["project_id"] = project_id
        elif project_id != task.project:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"project_id '{project_id}' does not match task's project '{task.project}'.",
            )

    if context_level == ContextLevel.GLOBAL and (project_id is not None or task_id is not None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Global sessions cannot have a project_id or task_id.",
        )
    if context_level == ContextLevel.PROJECT:
        if project_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Project-level sessions require a project_id.",
            )
        if task_id is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Project-level sessions cannot have a task_id.",
            )
    if context_level == ContextLevel.TASK and (project_id is None or task_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Task-level sessions require both project_id and task_id.",
        )

    session_data["context_level"] = context_level
    return session_data


@router.get("", response_model=list[SessionSchema])
def get_sessions(
    task_id: str | None = None,
    context_level: ContextLevel | None = None,
    project_id: str | None = None,
    status_: SessionStatus | None = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(SessionModel)
    if task_id:
        query = query.filter(SessionModel.task_id == task_id)
    if context_level:
        query = query.filter(SessionModel.context_level == context_level.value)
    if project_id:
        query = query.filter(SessionModel.project_id == project_id)
    if status_:
        query = query.filter(SessionModel.status == status_.value)
    return (
        query.order_by(SessionModel.pinned.desc(), SessionModel.last_activity_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.post("", response_model=SessionSchema, status_code=status.HTTP_201_CREATED)
def create_session(session_in: SessionCreate, db: Session = Depends(get_db)):
    session_data = session_in.model_dump(exclude_unset=True)
    session_data = _resolve_context(session_data, db)
    session_data = _validate_model_selection(session_data)

    messages = session_data.get("messages")
    session_data["message_count"] = len(messages) if messages else 0
    session_data["last_activity_at"] = datetime.now(timezone.utc)

    db_session = SessionModel(**session_data)
    db.add(db_session)
    db.commit()
    db.refresh(db_session)
    return db_session


@router.get("/{id}", response_model=SessionSchema)
def get_session(id: str, db: Session = Depends(get_db)):
    db_session = db.query(SessionModel).filter(SessionModel.id == id).first()
    if not db_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{id}' not found."
        )
    return db_session


@router.patch("/{id}", response_model=SessionSchema)
def update_session(id: str, session_in: SessionUpdate, db: Session = Depends(get_db)):
    db_session = db.query(SessionModel).filter(SessionModel.id == id).first()
    if not db_session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{id}' not found."
        )

    update_data = _validate_model_selection(
        session_in.model_dump(exclude_unset=True),
        db_session,
    )
    if "messages" in update_data:
        messages = update_data["messages"]
        update_data["message_count"] = len(messages) if messages else 0
        update_data["last_activity_at"] = datetime.now(timezone.utc)

    for field, value in update_data.items():
        setattr(db_session, field, value)

    db.commit()
    db.refresh(db_session)
    return db_session
