from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Session as SessionModel
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


@router.get("", response_model=list[SessionSchema])
def get_sessions(
    task_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(SessionModel)
    if task_id:
        query = query.filter(SessionModel.task_id == task_id)
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=SessionSchema, status_code=status.HTTP_201_CREATED)
def create_session(session_in: SessionCreate, db: Session = Depends(get_db)):
    session_data = _validate_model_selection(
        session_in.model_dump(exclude_unset=True)
    )
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
    for field, value in update_data.items():
        setattr(db_session, field, value)

    db.commit()
    db.refresh(db_session)
    return db_session
