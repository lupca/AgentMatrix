"""Unit tests for Telegram notification dispatcher (CTV2-1381)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.core.config import settings
from app.db.models import (
    NotificationDelivery,
    Project,
    Task,
    TaskEvent,
)
from app.services.notification_service import (
    TELEGRAM_EVENT_TYPES,
    claim,
    format_message,
    mark_skipped,
    record_outcome,
    select_pending_events,
    select_retryable_deliveries,
    select_stale_events,
)
from app.services.providers.telegram import send_message as tg_send


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def task_and_events(db_session):
    """Create a project, task, and events spanning whitelisted + non-whitelisted types.

    CTV2-1400: Telegram now whitelists exactly four event types
    (human_question, task_done, cost_brake, deadman). gate_pending/
    run_failed/escalated are still `kind='decision'` (they still matter to
    the coordinator's own digest), but must NOT reach Telegram any more.
    """
    uid = str(uuid.uuid4())[:8]
    proj = Project(id=f"proj-{uid}", name=f"Proj {uid}")
    task = Task(
        id=f"T-{uid}",
        project=proj.id,
        title=f"Task {uid}",
        status="dispatched",
    )
    db_session.add(proj)
    db_session.add(task)
    db_session.commit()

    gate_event = TaskEvent(
        task_id=task.id,
        event_type="gate_pending",
        kind="decision",
        payload={"gate": "review", "gate_record_id": 42},
    )
    run_failed_event = TaskEvent(
        task_id=task.id,
        event_type="run_failed",
        kind="decision",
        payload={"run_id": "run-abc", "error": "compilation failed"},
    )
    escalated_event = TaskEvent(
        task_id=task.id,
        event_type="escalated",
        kind="decision",
        payload={"reason": "unclear requirements"},
    )
    info_event = TaskEvent(
        task_id=task.id,
        event_type="task_created",
        kind="info",
        payload={},
    )
    human_question_event = TaskEvent(
        task_id=task.id,
        event_type="human_question",
        kind="decision",
        payload={"question": "Deploy now?", "why_human": "irreversible", "options": ["yes", "no"]},
    )
    task_done_event = TaskEvent(
        task_id=task.id,
        event_type="task_done",
        kind="decision",
        payload={"executor": "@codex", "reviewer": "@gemini", "commit": "abc123"},
    )
    cost_brake_event = TaskEvent(
        task_id=task.id,
        event_type="cost_brake",
        kind="decision",
        payload={"cost_usd": "50.00000000", "max_cost_usd_per_task": "50.00000000"},
    )
    deadman_event = TaskEvent(
        task_id=task.id,
        event_type="deadman",
        kind="decision",
        payload={"no_progress_minutes": 45, "reason": "no progress"},
    )
    for e in [
        gate_event, run_failed_event, escalated_event, info_event,
        human_question_event, task_done_event, cost_brake_event, deadman_event,
    ]:
        db_session.add(e)
    db_session.commit()

    return {
        "task": task,
        "gate": gate_event,
        "run_failed": run_failed_event,
        "escalated": escalated_event,
        "info": info_event,
        "human_question": human_question_event,
        "task_done": task_done_event,
        "cost_brake": cost_brake_event,
        "deadman": deadman_event,
    }


def _enable_telegram(monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "test-chat-id")
    monkeypatch.setattr(settings, "TELEGRAM_NOTIFY_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "TELEGRAM_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(settings, "TELEGRAM_MAX_EVENT_AGE_SECONDS", 3600)


def _ok_transport(message_id="123"):
    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})
    return httpx.MockTransport(handler)


def _fail_transport(status=500):
    def handler(request):
        return httpx.Response(status, json={"ok": False})
    return httpx.MockTransport(handler)


def _timeout_transport():
    def handler(request):
        raise httpx.ReadTimeout("timed out")
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. Only whitelisted events are selected (CTV2-1400)
# ---------------------------------------------------------------------------

def test_only_whitelisted_events_selected(db_session, task_and_events):
    """Exactly the four whitelisted types are selected; nothing else."""
    events = select_pending_events(db_session)
    event_types = {e.event_type for e in events}
    assert event_types == TELEGRAM_EVENT_TYPES
    assert event_types == {"human_question", "task_done", "cost_brake", "deadman"}
    assert len(events) == 4


def test_info_event_produces_zero_deliveries(db_session, task_and_events):
    """An info event is never selected for notification."""
    events = select_pending_events(db_session)
    assert task_and_events["info"].id not in {e.id for e in events}


def test_gate_pending_no_longer_reaches_telegram(db_session, task_and_events):
    """CTV2-1400: gate_pending was 553/day of the old noise -- cut."""
    events = select_pending_events(db_session)
    assert task_and_events["gate"].id not in {e.id for e in events}


def test_run_failed_and_escalated_no_longer_reach_telegram_directly(db_session, task_and_events):
    """Cut in favor of the coordinator's own `failed_work` channel (VIỆC 4
    verifies that channel still receives these -- see test_mcp_native.py)."""
    events = select_pending_events(db_session)
    ids = {e.id for e in events}
    assert task_and_events["run_failed"].id not in ids
    assert task_and_events["escalated"].id not in ids


def test_task_done_reaches_telegram(db_session, task_and_events):
    """`done` used to be kind='info' and never delivered -- now whitelisted."""
    events = select_pending_events(db_session)
    assert task_and_events["task_done"].id in {e.id for e in events}


def test_whitelist_is_exactly_four_types():
    assert TELEGRAM_EVENT_TYPES == {"human_question", "task_done", "cost_brake", "deadman"}


# ---------------------------------------------------------------------------
# 2. Message formatting
# ---------------------------------------------------------------------------

def test_format_message_gate_pending(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["gate"]
    token = "tok-gate-123"
    text = format_message(task, event, token)

    assert task.id in text
    assert task.title in text
    assert "gate_pending" in text
    assert "review" in text
    assert "42" in text
    assert token in text
    assert len(text) <= 4096


def test_format_message_run_failed(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["run_failed"]
    token = "tok-run-456"
    text = format_message(task, event, token)

    assert task.id in text
    assert "run_failed" in text
    assert "run-abc" in text
    assert "compilation failed" in text
    assert token in text
    assert len(text) <= 4096


def test_format_message_escalated(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["escalated"]
    token = "tok-esc-789"
    text = format_message(task, event, token)

    assert task.id in text
    assert "escalated" in text
    assert "unclear requirements" in text
    assert token in text
    assert len(text) <= 4096


def test_format_message_truncated_to_4096(task_and_events):
    task = task_and_events["task"]
    task.title = "x" * 5000
    event = task_and_events["escalated"]
    event.payload = {"reason": "x" * 5000}
    text = format_message(task, event, "token")
    assert len(text) <= 4096


def test_format_message_error_truncated_in_run_failed(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["run_failed"]
    event.payload = {"run_id": "r1", "error": "x" * 500}
    text = format_message(task, event, "token")
    assert "..." in text


def test_format_message_human_question(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["human_question"]
    text = format_message(task, event, "tok-ask-1")
    assert "Deploy now?" in text
    assert "irreversible" in text
    assert "yes" in text and "no" in text
    assert "tok-ask-1" in text


def test_format_message_human_question_without_task():
    """ask_human with no task_id: the message still renders, with no task line."""
    event = TaskEvent(
        task_id=None,
        event_type="human_question",
        kind="decision",
        payload={"question": "Restart the whole cluster?", "why_human": "irreversible", "options": []},
    )
    text = format_message(None, event, "tok-ask-2")
    assert "Restart the whole cluster?" in text
    assert "Task:" not in text
    assert "tok-ask-2" in text


def test_format_message_task_done(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["task_done"]
    text = format_message(task, event, "tok-done-1")
    assert task.id in text
    assert "@codex" in text
    assert "abc123" in text


def test_format_message_cost_brake(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["cost_brake"]
    text = format_message(task, event, "tok-cost-1")
    assert "50.00000000" in text


def test_format_message_deadman(task_and_events):
    task = task_and_events["task"]
    event = task_and_events["deadman"]
    text = format_message(task, event, "tok-dead-1")
    assert "45" in text


# ---------------------------------------------------------------------------
# 3. Claim idempotency
# ---------------------------------------------------------------------------

def test_claim_is_idempotent(db_session, task_and_events):
    """Two workers claiming the same event: only one succeeds."""
    event = task_and_events["gate"]
    d1 = claim(db_session, event)
    assert d1 is not None
    assert d1.status == "pending"

    d2 = claim(db_session, event)
    assert d2 is None


def test_claim_sets_correlation_token(db_session, task_and_events):
    event = task_and_events["gate"]
    d = claim(db_session, event)
    assert d.correlation_token is not None
    assert len(d.correlation_token) == 36


# ---------------------------------------------------------------------------
# 4. Telegram provider
# ---------------------------------------------------------------------------

def test_tg_send_success():
    transport = _ok_transport("999")
    ok, msg_id, error = tg_send("token", "chat", "hello", transport=transport)
    assert ok is True
    assert msg_id == "999"
    assert error is None


def test_tg_send_http_500():
    transport = _fail_transport(500)
    ok, msg_id, error = tg_send("token", "chat", "hello", transport=transport)
    assert ok is False
    assert msg_id is None
    assert "500" in error
    assert "token" not in error


def test_tg_send_timeout():
    transport = _timeout_transport()
    ok, msg_id, error = tg_send("token", "chat", "hello", timeout=1, transport=transport)
    assert ok is False
    assert "ReadTimeout" in error
    assert "token" not in error


def test_tg_send_timeout_is_explicit():
    """The request uses an explicit httpx.Timeout, not the default."""
    captured = {}

    def handler(request):
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    transport = httpx.MockTransport(handler)
    tg_send("token", "chat", "hello", timeout=7.0, transport=transport)
    assert captured["timeout"] is not None


# ---------------------------------------------------------------------------
# 5. No open transaction during send
# ---------------------------------------------------------------------------

def test_no_open_transaction_during_send(db_session, task_and_events, monkeypatch):
    """db.in_transaction() is False at the moment the transport is invoked."""
    from types import SimpleNamespace

    _enable_telegram(monkeypatch)
    event = task_and_events["gate"]
    task = task_and_events["task"]

    # Capture attributes BEFORE claim commits (which expires ORM objects)
    task_id, task_title = task.id, task.title
    event_type, event_payload = event.event_type, event.payload

    delivery = claim(db_session, event)
    assert delivery is not None
    d_id, d_token, d_attempts = delivery.id, delivery.correlation_token, delivery.attempts

    # Ensure no transaction is open after claim
    if db_session.in_transaction():
        db_session.rollback()

    # Use plain objects to avoid any ORM lazy loads during the send
    task_mock = SimpleNamespace(id=task_id, title=task_title)
    event_mock = SimpleNamespace(event_type=event_type, payload=event_payload)

    tx_states = []

    def checking_send(bot_token, chat_id, text, timeout, transport=None):
        tx_states.append(db_session.in_transaction())
        return True, "1", None

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", checking_send
    )
    monkeypatch.setattr(
        "app.services.notification_service.record_outcome",
        lambda *a, **kw: None,
    )

    from app.workers.notification_dispatcher import _send_one

    _send_one(
        delivery_id=d_id,
        correlation_token=d_token,
        attempts=d_attempts,
        task=task_mock,
        event=event_mock,
    )
    assert tx_states == [False]


# ---------------------------------------------------------------------------
# 6. Telegram failure does not affect task state
# ---------------------------------------------------------------------------

def test_telegram_failure_leaves_task_unchanged(
    db_session, task_and_events, monkeypatch
):
    """HTTP 500: task status/version unchanged, delivery row records failure."""
    _enable_telegram(monkeypatch)
    event = task_and_events["gate"]
    task = task_and_events["task"]
    original_status = task.status
    original_version = task.version

    delivery = claim(db_session, event)
    d_id, d_token, d_attempts = delivery.id, delivery.correlation_token, delivery.attempts

    def failing_send(bot_token, chat_id, text, timeout, transport=None):
        return False, None, "HTTP 500"

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", failing_send
    )

    outcome_recorded = {}

    def fake_record_outcome(delivery_id, *, status, **kw):
        outcome_recorded["status"] = status
        outcome_recorded["attempts"] = kw.get("attempts", 0)
        outcome_recorded["last_error"] = kw.get("last_error")

    monkeypatch.setattr(
        "app.services.notification_service.record_outcome", fake_record_outcome
    )
    monkeypatch.setattr(
        "app.workers.notification_dispatcher.record_outcome", fake_record_outcome
    )

    from app.workers.notification_dispatcher import _send_one

    _send_one(
        delivery_id=d_id,
        correlation_token=d_token,
        attempts=d_attempts,
        task=task,
        event=event,
    )

    db_session.refresh(task)
    assert task.status == original_status
    assert task.version == original_version
    assert outcome_recorded["status"] == "failed"
    assert outcome_recorded["attempts"] == 1
    assert outcome_recorded["last_error"] == "HTTP 500"


def test_timeout_delivery_recorded_as_failed(
    db_session, task_and_events, monkeypatch
):
    """httpx.ReadTimeout: delivery row at 'failed' with last_error populated."""
    _enable_telegram(monkeypatch)
    event = task_and_events["run_failed"]
    task = task_and_events["task"]
    delivery = claim(db_session, event)
    d_id, d_token, d_attempts = delivery.id, delivery.correlation_token, delivery.attempts

    outcome = {}

    def timeout_send(bot_token, chat_id, text, timeout, transport=None):
        return False, None, "timeout: ReadTimeout"

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", timeout_send
    )
    monkeypatch.setattr(
        "app.workers.notification_dispatcher.record_outcome",
        lambda did, **kw: outcome.update({"id": did, **kw}),
    )

    from app.workers.notification_dispatcher import _send_one

    _send_one(
        delivery_id=d_id,
        correlation_token=d_token,
        attempts=d_attempts,
        task=task,
        event=event,
    )
    assert outcome["status"] == "failed"
    assert "ReadTimeout" in outcome["last_error"]


# ---------------------------------------------------------------------------
# 7. Retry bounded at TELEGRAM_MAX_ATTEMPTS
# ---------------------------------------------------------------------------

def _patch_session_local(monkeypatch, db_session):
    """Make SessionLocal in dispatcher + notification_service return the test session."""
    from app.db.base import SessionLocal as _RealSessionLocal
    from sqlalchemy.orm import sessionmaker

    test_session_factory = sessionmaker(bind=db_session.bind)

    def _test_session_local():
        return test_session_factory()

    monkeypatch.setattr(
        "app.workers.notification_dispatcher.SessionLocal", _test_session_local
    )
    monkeypatch.setattr(
        "app.services.notification_service.SessionLocal", _test_session_local
    )


def test_retry_bounded_at_max_attempts(db_session, monkeypatch):
    """After 3 failed attempts, no more transport calls — even after 5 ticks."""
    _enable_telegram(monkeypatch)
    _patch_session_local(monkeypatch, db_session)

    uid = str(uuid.uuid4())[:8]
    proj = Project(id=f"proj-{uid}", name=f"Proj {uid}")
    task = Task(id=f"T-{uid}", project=proj.id, title="Retry task", status="dispatched")
    db_session.add(proj)
    db_session.add(task)
    db_session.commit()

    event = TaskEvent(
        task_id=task.id,
        event_type="task_done",
        kind="decision",
        payload={"executor": "@codex", "commit": "abc"},
    )
    db_session.add(event)
    db_session.commit()
    event_id = event.id

    call_count = 0

    def failing_send(bot_token, chat_id, text, timeout, transport=None):
        nonlocal call_count
        call_count += 1
        return False, None, "HTTP 500"

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", failing_send
    )

    from app.workers.notification_dispatcher import poll_tick

    for _ in range(5):
        poll_tick()

    assert call_count == 3

    d = db_session.query(NotificationDelivery).filter_by(task_event_id=event_id).first()
    assert d is not None
    assert d.status == "failed"
    assert d.attempts == 3


# ---------------------------------------------------------------------------
# 8. Disabled config → zero HTTP calls
# ---------------------------------------------------------------------------

def test_disabled_config_zero_http_calls(db_session, task_and_events, monkeypatch):
    """Empty bot token → actor is a no-op, zero HTTP calls."""
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(settings, "TELEGRAM_NOTIFY_ENABLED", True)

    send_called = False

    def should_not_be_called(*a, **kw):
        nonlocal send_called
        send_called = True
        return True, "1", None

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", should_not_be_called
    )

    from app.workers.notification_dispatcher import poll_tick

    result = poll_tick()
    assert result["dispatched"] == 0
    assert send_called is False


def test_notify_enabled_false_zero_http_calls(db_session, task_and_events, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(settings, "TELEGRAM_NOTIFY_ENABLED", False)

    send_called = False

    def should_not_be_called(*a, **kw):
        nonlocal send_called
        send_called = True
        return True, "1", None

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", should_not_be_called
    )

    from app.workers.notification_dispatcher import poll_tick

    result = poll_tick()
    assert result["dispatched"] == 0
    assert send_called is False


# ---------------------------------------------------------------------------
# 9. Stale events recorded as skipped
# ---------------------------------------------------------------------------

def test_stale_events_recorded_as_skipped(db_session, monkeypatch):
    """Events older than TELEGRAM_MAX_EVENT_AGE_SECONDS → status='skipped'."""
    _enable_telegram(monkeypatch)
    _patch_session_local(monkeypatch, db_session)
    monkeypatch.setattr(settings, "TELEGRAM_MAX_EVENT_AGE_SECONDS", 60)

    uid = str(uuid.uuid4())[:8]
    proj = Project(id=f"proj-{uid}", name=f"Proj {uid}")
    task = Task(id=f"T-{uid}", project=proj.id, title="Old task", status="dispatched")
    db_session.add(proj)
    db_session.add(task)
    db_session.commit()

    old_event = TaskEvent(
        task_id=task.id,
        event_type="task_done",
        kind="decision",
        payload={"executor": "@codex", "commit": "abc"},
    )
    db_session.add(old_event)
    db_session.commit()

    old_event.created_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    db_session.commit()

    send_called = False

    def should_not_be_called(*a, **kw):
        nonlocal send_called
        send_called = True
        return True, "1", None

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", should_not_be_called
    )

    from app.workers.notification_dispatcher import poll_tick

    poll_tick()

    assert send_called is False
    d = db_session.query(NotificationDelivery).filter_by(task_event_id=old_event.id).first()
    assert d is not None
    assert d.status == "skipped"


# ---------------------------------------------------------------------------
# 10. Successful send records sent status
# ---------------------------------------------------------------------------

def test_successful_send_records_sent(db_session, task_and_events, monkeypatch):
    _enable_telegram(monkeypatch)
    event = task_and_events["gate"]
    task = task_and_events["task"]
    delivery = claim(db_session, event)
    d_id, d_token, d_attempts = delivery.id, delivery.correlation_token, delivery.attempts

    outcome = {}

    def ok_send(bot_token, chat_id, text, timeout, transport=None):
        return True, "msg-42", None

    monkeypatch.setattr(
        "app.workers.notification_dispatcher._tg_send", ok_send
    )
    monkeypatch.setattr(
        "app.workers.notification_dispatcher.record_outcome",
        lambda did, **kw: outcome.update({"id": did, **kw}),
    )

    from app.workers.notification_dispatcher import _send_one

    _send_one(
        delivery_id=d_id,
        correlation_token=d_token,
        attempts=d_attempts,
        task=task,
        event=event,
    )
    assert outcome["status"] == "sent"
    assert outcome["provider_message_id"] == "msg-42"
    assert outcome["sent_at"] is not None


# ---------------------------------------------------------------------------
# 11. Stale events query
# ---------------------------------------------------------------------------

def test_select_stale_events(db_session, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_MAX_EVENT_AGE_SECONDS", 60)

    uid = str(uuid.uuid4())[:8]
    proj = Project(id=f"proj-{uid}", name=f"Proj {uid}")
    task = Task(id=f"T-{uid}", project=proj.id, title="T", status="dispatched")
    db_session.add(proj)
    db_session.add(task)
    db_session.commit()

    old = TaskEvent(
        task_id=task.id, event_type="task_done", kind="decision",
        payload={"executor": "@codex", "commit": "abc"},
    )
    fresh = TaskEvent(
        task_id=task.id, event_type="cost_brake", kind="decision",
        payload={"cost_usd": "50", "max_cost_usd_per_task": "50"},
    )
    db_session.add_all([old, fresh])
    db_session.commit()

    old.created_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    db_session.commit()

    stale = select_stale_events(db_session)
    assert len(stale) == 1
    assert stale[0].id == old.id

    pending = select_pending_events(db_session)
    assert len(pending) == 1
    assert pending[0].id == fresh.id


# ---------------------------------------------------------------------------
# 12. select_retryable_deliveries
# ---------------------------------------------------------------------------

def test_select_retryable_deliveries(db_session, task_and_events, monkeypatch):
    monkeypatch.setattr(settings, "TELEGRAM_MAX_ATTEMPTS", 3)
    event = task_and_events["gate"]
    d = claim(db_session, event)
    # Update the DB row directly (claim returns a frozen DeliveryClaim)
    row = db_session.query(NotificationDelivery).filter_by(id=d.id).first()
    row.status = "failed"
    row.attempts = 1
    db_session.commit()

    retryable = select_retryable_deliveries(db_session)
    assert len(retryable) == 1
    assert retryable[0].id == d.id

    row.attempts = 3
    db_session.commit()
    retryable = select_retryable_deliveries(db_session)
    assert len(retryable) == 0
