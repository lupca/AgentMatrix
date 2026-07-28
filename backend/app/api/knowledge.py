import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import KnowledgeItem as KnowledgeItemModel
from app.schemas.knowledge import KnowledgeItem, KnowledgeItemCreate, KnowledgeItemUpdate
from app.db.archive import with_archived
from app.services.archive import ArchiveError, ArchiveService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


@router.get("", response_model=list[KnowledgeItem])
def get_knowledge_items(
    category: str | None = None,
    project: str | None = None,
    search: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False),
    db: Session = Depends(get_db)
):
    query = with_archived(db, KnowledgeItemModel, include_archived)
    if category:
        query = query.filter(KnowledgeItemModel.category == category)
    if project:
        query = query.filter(KnowledgeItemModel.project == project)
    if search:
        query = query.filter(
            or_(
                KnowledgeItemModel.title.ilike(f"%{search}%"),
                KnowledgeItemModel.content.ilike(f"%{search}%")
            )
        )
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=KnowledgeItem, status_code=status.HTTP_201_CREATED)
def create_knowledge_item(item_in: KnowledgeItemCreate, db: Session = Depends(get_db)):
    item_data = item_in.model_dump(exclude_unset=True)

    if not item_data.get("id"):
        item_data["id"] = f"k-{uuid.uuid4().hex[:8]}"
    else:
        existing = db.query(KnowledgeItemModel).filter(KnowledgeItemModel.id == item_data["id"]).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Knowledge item with ID '{item_data['id']}' already exists."
            )

    db_item = KnowledgeItemModel(**item_data)
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return db_item


@router.get("/{id}", response_model=KnowledgeItem)
def get_knowledge_item(id: str, include_archived: bool = Query(False), db: Session = Depends(get_db)):
    db_item = with_archived(db, KnowledgeItemModel, include_archived).filter(KnowledgeItemModel.id == id).first()
    if not db_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge item '{id}' not found."
        )
    return db_item


@router.patch("/{id}", response_model=KnowledgeItem)
def update_knowledge_item(id: str, item_in: KnowledgeItemUpdate, db: Session = Depends(get_db)):
    db_item = db.query(KnowledgeItemModel).filter(KnowledgeItemModel.id == id).first()
    if not db_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Knowledge item '{id}' not found."
        )

    update_data = item_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_item, field, value)

    db.commit()
    db.refresh(db_item)
    return db_item


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_knowledge_item(id: str, db: Session = Depends(get_db)):
    try:
        ArchiveService(db, "rest:knowledge").archive("knowledge", id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return None


@router.post("/{id}/archive")
def archive_knowledge_item(id: str, db: Session = Depends(get_db)):
    try:
        return ArchiveService(db, "rest:knowledge").archive("knowledge", id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{id}/restore")
def restore_knowledge_item(id: str, db: Session = Depends(get_db)):
    try:
        return ArchiveService(db, "rest:knowledge").restore("knowledge", id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
