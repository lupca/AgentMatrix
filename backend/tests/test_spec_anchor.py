import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import inspect

from app.db.models import OutboxEvent, Project, SpecAnchor, SpecItem
from app.services.command_router import CommandRouter
from app.services.outbox import publish_pending_events
from app.services.spec_anchor import (
    apply_commit_staleness,
    compute_anchor_sha,
    extract_python_symbol_source,
    hash_symbol_source,
)
from app.services.spec_service import SpecError, write_specs


MODULE_V1 = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
MODULE_V2_FOO_CHANGED = "def foo():\n    return 999\n\ndef bar():\n    return 2\n"
MODULE_V3_BAR_CHANGED = "def foo():\n    return 1\n\ndef bar():\n    return 999\n"


def _write_module(repo_root: str, body: str) -> None:
    (Path(repo_root) / "mod.py").write_text(body)


def _commit(repo_root: str, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_root, check=True, capture_output=True)
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


@pytest.fixture
def anchored_item(db_session, git_repo_root):
    db_session.add(Project(id="anchor-proj", name="Anchor project", repo_root=git_repo_root))
    db_session.commit()

    _write_module(git_repo_root, MODULE_V1)
    _commit(git_repo_root, "add mod.py")

    return write_specs(db_session, [
        {
            "op": "create", "id": "spec-anchor-item", "project_id": "anchor-proj",
            "kind": "constraint", "title": "foo returns 1", "body": "foo() must return 1",
            "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "spec-anchor-item", "repo": git_repo_root,
            "path": "mod.py", "symbol": "foo", "relation": "implements",
        },
    ])


def test_spec_anchor_table_registered(db_session):
    inspector = inspect(db_session.bind)
    assert "spec_anchor" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("spec_anchor")}
    assert {"id", "spec_item_id", "repo", "path", "symbol", "relation", "anchor_sha"} <= columns
    assert "stale_reason" in {c["name"] for c in inspector.get_columns("spec_item")}


def test_anchor_op_hashes_symbol_content_from_repo(anchored_item, db_session, git_repo_root):
    assert anchored_item["anchors"], "anchor op should report the written anchor"
    written = anchored_item["anchors"][0]
    expected = compute_anchor_sha(git_repo_root, "mod.py", "foo")
    assert written["anchor_sha"] == expected
    assert written["relation"] == "implements"

    row = db_session.query(SpecAnchor).filter_by(spec_item_id="spec-anchor-item").one()
    assert row.repo == git_repo_root
    assert row.path == "mod.py"
    assert row.symbol == "foo"
    assert row.anchor_sha == expected


def test_python_anchor_supports_assignments_and_class_attributes(tmp_path):
    source = """MAX_COST = 10

class Config:
    LIMIT: int = 3
"""
    assert extract_python_symbol_source(source, "MAX_COST") == "MAX_COST = 10"
    assert extract_python_symbol_source(source, "Config.LIMIT") == "LIMIT: int = 3"
    assert extract_python_symbol_source(source, "missing") is None

    path = tmp_path / "settings.py"
    path.write_text(source)
    assert compute_anchor_sha(str(tmp_path), "settings.py", "MAX_COST") == hash_symbol_source(
        "MAX_COST = 10"
    )


def test_non_python_anchor_hashes_whole_file_and_staleness_uses_same_mode(
    db_session, git_repo_root
):
    config_path = Path(git_repo_root) / "nginx.conf"
    config_path.write_text("client_max_body_size 10m;\nkeep = true\n")
    _commit(git_repo_root, "add config")

    db_session.add(Project(id="config-anchor-proj", name="Config project", repo_root=git_repo_root))
    db_session.commit()
    written = write_specs(db_session, [
        {
            "op": "create", "id": "config-anchor-item", "project_id": "config-anchor-proj",
            "kind": "constraint", "title": "config", "body": "config must stay valid",
            "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "config-anchor-item", "repo": git_repo_root,
            "path": "nginx.conf", "symbol": "client_max_body_size", "relation": "constrains",
        },
    ])
    assert written["anchors"][0]["anchor_sha"] == compute_anchor_sha(
        git_repo_root, "nginx.conf", "client_max_body_size"
    )

    config_path.write_text("client_max_body_size 10m;\nkeep = false\n")
    head = _commit(git_repo_root, "change unrelated config line")
    result = apply_commit_staleness(db_session, "config-anchor-proj", git_repo_root, head)

    item = db_session.get(SpecItem, "config-anchor-item")
    assert item.status == "stale"
    assert result["staled"][0]["reason"].startswith("file 'client_max_body_size'")


def test_anchor_op_ignores_supplied_hash_when_source_is_available(
    anchored_item, db_session, git_repo_root
):
    result = write_specs(db_session, [
        {
            "op": "create", "id": "spec-anchor-server-wins", "project_id": "anchor-proj",
            "kind": "constraint", "title": "server hash", "body": "server computes it",
        },
        {
            "op": "anchor", "spec_item_id": "spec-anchor-server-wins", "repo": git_repo_root,
            "path": "mod.py", "symbol": "foo", "relation": "implements",
            "anchor_sha": "a" * 64,
        },
    ])

    assert result["anchors"][0]["anchor_sha"] == compute_anchor_sha(git_repo_root, "mod.py", "foo")
    assert result["anchors"][0]["anchor_sha"] != "a" * 64


def test_anchor_op_rejects_commit_sha_even_when_source_is_available(
    anchored_item, db_session, git_repo_root
):
    with pytest.raises(SpecError, match="exactly 64 hexadecimal characters"):
        write_specs(db_session, [
            {
                "op": "create", "id": "spec-anchor-invalid", "project_id": "anchor-proj",
                "kind": "constraint", "title": "invalid hash", "body": "reject commit SHA",
            },
            {
                "op": "anchor", "spec_item_id": "spec-anchor-invalid", "repo": git_repo_root,
                "path": "mod.py", "symbol": "foo", "relation": "implements",
                "anchor_sha": "b" * 40,
            },
        ])


def test_anchor_op_rejects_unresolved_python_symbol_even_with_manual_hash(
    anchored_item, db_session, git_repo_root
):
    with pytest.raises(SpecError, match="could not resolve Python symbol"):
        write_specs(db_session, [
            {
                "op": "create", "id": "spec-anchor-unresolved-python", "project_id": "anchor-proj",
                "kind": "constraint", "title": "invalid Python symbol", "body": "reject it",
            },
            {
                "op": "anchor", "spec_item_id": "spec-anchor-unresolved-python", "repo": git_repo_root,
                "path": "mod.py", "symbol": "not_a_declaration", "relation": "implements",
                "anchor_sha": "b" * 64,
            },
        ])


def test_commit_touching_anchored_symbol_marks_item_stale(anchored_item, db_session, git_repo_root):
    _write_module(git_repo_root, MODULE_V2_FOO_CHANGED)
    head = _commit(git_repo_root, "change foo")

    result = apply_commit_staleness(db_session, "anchor-proj", git_repo_root, head)

    item = db_session.get(SpecItem, "spec-anchor-item")
    assert item.status == "stale"
    assert "foo" in item.stale_reason
    assert head in item.stale_reason
    assert result["staled"] == [
        {
            "spec_item_id": "spec-anchor-item", "symbol": "foo", "path": "mod.py",
            "reason": item.stale_reason,
        }
    ]


def test_commit_not_touching_anchored_symbol_leaves_status(anchored_item, db_session, git_repo_root):
    _write_module(git_repo_root, MODULE_V3_BAR_CHANGED)
    head = _commit(git_repo_root, "change bar, not foo")

    result = apply_commit_staleness(db_session, "anchor-proj", git_repo_root, head)

    item = db_session.get(SpecItem, "spec-anchor-item")
    assert item.status == "active"
    assert item.stale_reason is None
    assert result["staled"] == []


def test_commit_touching_unrelated_file_leaves_status(anchored_item, db_session, git_repo_root):
    (Path(git_repo_root) / "unrelated.txt").write_text("noise")
    head = _commit(git_repo_root, "unrelated change")

    result = apply_commit_staleness(db_session, "anchor-proj", git_repo_root, head)

    item = db_session.get(SpecItem, "spec-anchor-item")
    assert item.status == "active"
    assert result == {"checked": 0, "staled": []}


def test_apply_commit_staleness_is_idempotent_on_replay(anchored_item, db_session, git_repo_root):
    _write_module(git_repo_root, MODULE_V2_FOO_CHANGED)
    head = _commit(git_repo_root, "change foo")

    first = apply_commit_staleness(db_session, "anchor-proj", git_repo_root, head)
    item = db_session.get(SpecItem, "spec-anchor-item")
    reason_after_first = item.stale_reason

    second = apply_commit_staleness(db_session, "anchor-proj", git_repo_root, head)
    db_session.refresh(item)

    assert first["staled"] == second["staled"]
    assert item.status == "stale"
    assert item.stale_reason == reason_after_first


def test_no_llm_call_anywhere_in_the_module():
    import app.services.spec_anchor as mod
    import inspect as inspect_mod

    source = inspect_mod.getsource(mod)
    for banned in ("llm_service", "openai", "anthropic", "llm_call", "chat_completion"):
        assert banned not in source.lower()


def test_spec_stale_tool_lists_symbol_and_commit(anchored_item, db_session, git_repo_root):
    _write_module(git_repo_root, MODULE_V2_FOO_CHANGED)
    head = _commit(git_repo_root, "change foo")
    apply_commit_staleness(db_session, "anchor-proj", git_repo_root, head)

    router = CommandRouter(db_session)
    result = asyncio.run(router.execute_tool("spec_stale", {"project": "anchor-proj"}, "session-1"))

    assert result["count"] == 1
    assert result["items"][0]["id"] == "spec-anchor-item"
    assert "foo" in result["items"][0]["reason"]
    assert head in result["items"][0]["reason"]


def test_spec_stale_tool_requires_project():
    from app.services.tool_registry import TOOL_REGISTRY

    assert TOOL_REGISTRY["spec_stale"].required_role == "executor"
    assert TOOL_REGISTRY["spec_stale"].group == "spec"


def test_outbox_graph_rebuild_event_drives_spec_staleness(db_session, git_repo_root):
    db_session.add(Project(id="anchor-proj-2", name="P2", repo_root=git_repo_root, graph_status="idle"))
    db_session.add(OutboxEvent(
        event_type="graph_rebuild_requested",
        payload={"project_id": "anchor-proj-2", "repo_root": git_repo_root, "commit_sha": "deadbeef"},
    ))
    db_session.commit()

    with patch("app.services.outbox.rebuild_graph_incremental_sync", return_value={}), \
         patch("app.services.outbox.apply_commit_staleness") as mock_staleness:
        publish_pending_events(db_session)

    mock_staleness.assert_called_once_with(db_session, "anchor-proj-2", git_repo_root, "deadbeef")
