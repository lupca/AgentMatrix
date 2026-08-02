"""Output parsing and event translation for CLI agents."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    AGENT_EVENT_TYPES,
    AgentEvent,
    AgentOutputChunk,
    VendorRawEvent,
)
from app.schemas.task import ReviewResult
from app.services.command_builder import review_result_path
from app.services.tool_metrics import record_tool_metric

OUTPUT_CHUNK_LINES = max(1, int(os.getenv("AGENT_OUTPUT_CHUNK_LINES", "100")))


class ReviewResultLoadError(ValueError):
    """Structured failure while loading a code-review result artifact."""

    def __init__(self, code: str, path: str, message: str, **details):
        self.code = code
        self.path = path
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, "path": self.path, "details": self.details}


def load_review_result(
    repo_root: str,
    task_id: str,
    acceptance_criteria: list | None = None,
) -> ReviewResult:
    """Read and strictly validate the review artifact written by the agent."""
    path = review_result_path(repo_root, task_id)
    try:
        with open(path, encoding="utf-8") as result_file:
            raw = result_file.read()
    except FileNotFoundError as exc:
        raise ReviewResultLoadError(
            "missing_file", path, "Review result file is missing"
        ) from exc
    except OSError as exc:
        raise ReviewResultLoadError(
            "read_error", path, "Review result file could not be read", error=str(exc)
        ) from exc

    if not raw.strip():
        raise ReviewResultLoadError("empty_file", path, "Review result file is empty")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewResultLoadError(
            "invalid_json", path, "Review result is not valid JSON", line=exc.lineno,
            column=exc.colno,
        ) from exc

    try:
        result = ReviewResult.model_validate(payload)
    except ValidationError as exc:
        error_types = {error.get("type") for error in exc.errors()}
        code = "missing_required_field" if "missing" in error_types else (
            "invalid_type"
            if any(
                isinstance(error_type, str) and "type" in error_type
                for error_type in error_types
            )
            else "schema_validation"
        )
        raise ReviewResultLoadError(
            code, path, "Review result does not match its schema",
            errors=exc.errors(),
        ) from exc

    if result.task_id != task_id:
        raise ReviewResultLoadError(
            "task_id_mismatch", path, "Review result task_id does not match the run",
            expected=task_id, actual=result.task_id,
        )
    expected_count = len(acceptance_criteria or [])
    if len(result.ac_results) != expected_count:
        raise ReviewResultLoadError(
            "acceptance_criteria_count_mismatch",
            path,
            "Review result must contain one result per acceptance criterion",
            expected=expected_count,
            actual=len(result.ac_results),
        )
    record_tool_metric(
        tool="review_result",
        source="agent_runner",
        ok=True,
        task_id=task_id,
        result_count=len(result.findings),
        bytes_out=len(raw),
        payload={
            "ac_pass": sum(1 for a in result.ac_results if a.status == "pass"),
            "ac_fail": sum(1 for a in result.ac_results if a.status == "fail"),
            "findings_by_severity": _findings_by_severity(result),
            "tests_run": len(result.tests_run),
            "tests_passed": len(result.tests_passed),
        },
    )
    return result


def _findings_by_severity(result: ReviewResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in result.findings:
        key = (finding.severity or "unknown").lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def _json_line(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _vendor_event(event_type: str, payload: dict[str, Any], timestamp: datetime | None = None) -> dict:
    if event_type not in AGENT_EVENT_TYPES:
        return {}
    return {
        "event_type": event_type,
        "timestamp": timestamp or datetime.now(timezone.utc),
        "payload": payload,
    }


def parse_vendor_event(cli: str, line: str) -> list[dict]:
    """Translate one line from claude, agy, or codex into common events."""
    vendor = (cli or "").strip().lower()
    raw = _json_line(line)
    if raw is None:
        return [_vendor_event("llm.completed", {"text": line, "stream": "stdout"})]

    raw_timestamp = raw.get("timestamp") or raw.get("created_at")
    timestamp = None
    if isinstance(raw_timestamp, str):
        try:
            timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except ValueError:
            pass
    kind = str(raw.get("type") or raw.get("event") or raw.get("status") or "").lower()
    item = raw.get("item") if isinstance(raw.get("item"), dict) else raw
    item_kind = str(item.get("type") or "").lower()

    if vendor == "codex":
        mapping = {
            "thread.started": "run.started",
            "turn.started": "llm.requested",
            "turn.completed": "llm.completed",
            "turn.failed": "run.completed",
            "item.started": "tool.started",
            "item.completed": "tool.completed",
        }
    elif vendor == "claude":
        mapping = {
            "system": "run.started",
            "assistant": "llm.completed",
            "result": "run.completed",
            "tool_use": "tool.started",
            "tool_result": "tool.completed",
        }
    else:  # agy / Gemini-style JSONL
        mapping = {
            "start": "run.started",
            "started": "run.started",
            "request": "llm.requested",
            "response": "llm.completed",
            "complete": "run.completed",
            "completed": "run.completed",
            "tool_call": "tool.started",
            "tool_result": "tool.completed",
        }

    normalized_type = mapping.get(kind) or mapping.get(item_kind)
    if normalized_type is None and any(key in raw for key in ("tool", "tool_name", "tool_use")):
        normalized_type = "tool.completed" if "result" in kind else "tool.started"
    if normalized_type is None:
        normalized_type = "llm.completed"
    return [_vendor_event(normalized_type, raw, timestamp)]


# Descriptive aliases for callers that use the adapter terminology.
normalize_cli_event = parse_vendor_event
parse_cli_output = parse_vendor_event


_EXPLICIT_RESULT_REF = re.compile(
    r"^\s*(?:result[_-]ref|result reference)\s*:\s*([^\s]+)\s*$",
    re.IGNORECASE,
)


def _extract_explicit_result_ref(line: str) -> str | None:
    """Read the optional commit ref convention emitted by an executor."""
    match = _EXPLICIT_RESULT_REF.match(line)
    return match.group(1) if match else None


def _record_agent_event(
    db: Session,
    run_id: str,
    seq: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> None:
    db.add(
        AgentEvent(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            timestamp=timestamp or datetime.now(timezone.utc),
            payload=payload or {},
        )
    )


def _record_vendor_output(
    db: Session, run_id: str, cli: str, raw_seq: int, event_seq: int, line: str
) -> int:
    """Persist the raw line and its normalized events; return next event seq."""
    db.add(VendorRawEvent(run_id=run_id, seq=raw_seq, cli=cli or "unknown", raw_output=line))
    next_seq = event_seq
    for event in parse_vendor_event(cli, line):
        _record_agent_event(
            db,
            run_id,
            next_seq,
            event["event_type"],
            event["payload"],
            event["timestamp"],
        )
        next_seq += 1
    return next_seq


def _next_agent_event_seq(db: Session, run_id: str) -> int:
    latest = (
        db.query(AgentEvent.seq)
        .filter(AgentEvent.run_id == run_id)
        .order_by(AgentEvent.seq.desc())
        .first()
    )
    return (latest[0] + 1) if latest else 0


def _next_chunk_index(db: Session, run_id: str) -> int:
    highest = (
        db.query(func.max(AgentOutputChunk.chunk_index))
        .filter(AgentOutputChunk.run_id == run_id)
        .scalar()
    )
    return 0 if highest is None else highest + 1
