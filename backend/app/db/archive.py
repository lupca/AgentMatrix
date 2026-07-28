from sqlalchemy.orm import Session


def active_query(db: Session, model):
    """Return a query containing only active rows for an archivable model."""
    return db.query(model).filter(model.archived_at.is_(None))


def with_archived(db: Session, model, include_archived: bool = False):
    """Return active rows by default, or all rows when explicitly requested."""
    query = db.query(model)
    return query if include_archived else query.filter(model.archived_at.is_(None))


def archived_only(db: Session, model):
    """Return only archived rows for an archivable model."""
    return db.query(model).filter(model.archived_at.isnot(None))
