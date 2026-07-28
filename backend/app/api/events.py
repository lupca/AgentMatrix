from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.schemas.events import EventsPollResponse, TaskEventResponse
from app.services.task_event_service import get_task_events

router = APIRouter(prefix="/api/events", tags=["events"])


def _parse_cursor(value: str) -> datetime:
    """Parse an ISO-8601 polling cursor, accepting URL-decoded UTC offsets."""
    normalized = value.strip().replace(" ", "+")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid 'since' timestamp format. Must be an ISO 8601 string.",
        ) from exc


def _format_cursor(value: datetime) -> str:
    """Return a canonical UTC cursor so browsers can send it back unchanged."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@router.get("", response_model=EventsPollResponse)
def poll_events(
    since: str | None = Query(
        None,
        description="ISO timestamp cursor to fetch events created after",
    ),
    task_id: str | None = Query(None, description="Filter by task ID"),
    types: str | None = Query(
        None,
        description="Comma-separated event types to filter",
    ),
    limit: int = Query(
        50,
        ge=1,
        le=200,
        description="Max number of events to return",
    ),
    db: Session = Depends(get_db),
) -> EventsPollResponse:
    """
    Poll task events with optional filters (since, task_id, types).
    Returns events, new cursor timestamp, and has_more flag.
    """
    parsed_since = _parse_cursor(since) if since else None

    event_types: list[str] | None = None
    if types:
        event_types = [t.strip() for t in types.split(",") if t.strip()]

    # Query limit + 1 to calculate has_more
    raw_events = get_task_events(
        db=db,
        task_id=task_id,
        since=parsed_since,
        event_types=event_types,
        limit=limit + 1,
    )

    has_more = len(raw_events) > limit
    events = raw_events[:limit]

    if events:
        cursor = _format_cursor(events[-1].created_at)
    elif parsed_since:
        cursor = _format_cursor(parsed_since)
    else:
        cursor = _format_cursor(datetime.now(timezone.utc))

    return EventsPollResponse(
        events=[TaskEventResponse.model_validate(e) for e in events],
        cursor=cursor,
        has_more=has_more,
    )
