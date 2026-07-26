from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import AuditLog as AuditLogModel
from app.schemas.audit import AuditLog

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[AuditLog])
def get_audit_logs(
    task_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(AuditLogModel)
    if task_id:
        query = query.filter(AuditLogModel.task_id == task_id)
    return query.order_by(AuditLogModel.created_at.desc()).offset(offset).limit(limit).all()
