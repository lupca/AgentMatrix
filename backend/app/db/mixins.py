from datetime import datetime, timezone

from sqlalchemy import Column, DateTime


class ArchivableMixin:
    """Common soft-delete state for user-owned entities."""

    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def archive(self) -> None:
        self.archived_at = datetime.now(timezone.utc)

    def restore(self) -> None:
        self.archived_at = None
