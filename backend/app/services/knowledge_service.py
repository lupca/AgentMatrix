from sqlalchemy.orm import Session
from app.db.models import KnowledgeItem


class KnowledgeService:
    def __init__(self, db: Session):
        self.db = db

    def get_relevant(self, project: str, gate: str) -> list:
        return self.db.query(KnowledgeItem).filter(
            KnowledgeItem.project == project
        ).all()
