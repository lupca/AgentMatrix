import asyncio
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db.models import Project, SpecItem, SpecRelation, SpecTaskLink, Task
from app.services.command_router import CommandRouter
from app.services.spec_service import SpecError, get_specs, write_specs


def test_living_spec_tables_are_registered(db_session):
    inspector = inspect(db_session.bind)
    tables = inspector.get_table_names()
    assert "spec_item" in tables
    assert "spec_relation" in tables
    assert "spec_task_link" in tables
    assert {
        "id", "project_id", "kind", "title", "body", "status", "supersedes_id",
        "source_doc_id", "derived_from_sha", "derived_by", "confidence", "verified_at",
        "verified_by", "embedding", "archived_at",
    } <= {column["name"] for column in inspector.get_columns("spec_item")}
    assert {"from_id", "to_id", "kind"} <= {
        column["name"] for column in inspector.get_columns("spec_relation")
    }
    assert {
        "id", "spec_item_id", "task_id", "relation", "confidence",
        "created_by", "created_at",
    } <= {column["name"] for column in inspector.get_columns("spec_task_link")}
    assert any(
        constraint["column_names"] == ["spec_item_id", "task_id", "relation"]
        for constraint in inspector.get_unique_constraints("spec_task_link")
    )
    assert "realization" in {column["name"] for column in inspector.get_columns("spec_item")}


def test_spec_task_links_write_and_read_in_both_directions(db_session):
    from app.db.models import Project, Task

    project = Project(id="linked-project", name="Linked project")
    task = Task(id="LINK-1", project=project.id, title="Implement linked specs")
    later_task = Task(id="LINK-2", project=project.id, title="Implement the spec again")
    db_session.add_all([project, task, later_task])
    db_session.commit()
    router = CommandRouter(db_session)
    relations = ("implements", "modifies", "violates", "references")
    ops = []
    for relation in relations:
        spec_id = f"spec-{relation}"
        ops.extend([
            {
                "op": "create", "id": spec_id, "project_id": project.id,
                "kind": "requirement", "title": relation, "body": f"{relation} behavior",
            },
            {
                "op": "task_link", "spec_item_id": spec_id, "task_id": task.id,
                "relation": relation, "confidence": "verified", "created_by": "@planner",
            },
        ])
    ops.append({
        "op": "task_link", "spec_item_id": "spec-implements", "task_id": later_task.id,
        "relation": "implements", "confidence": "derived", "created_by": "@later-planner",
    })

    written = asyncio.run(router.execute_tool(
        "spec_write", {"ops": ops}, "session-1"
    ))

    assert written["action"] == "spec_written"
    assert {link["relation"] for link in written["task_links"]} == set(relations)
    assert db_session.query(SpecTaskLink).count() == 5

    # task -> specs: task_id is a real client-facing spec_get selector.
    by_task = asyncio.run(router.execute_tool(
        "spec_get", {"task_id": task.id}, "session-1"
    ))
    assert {item["id"] for item in by_task["items"]} == {
        f"spec-{relation}" for relation in relations
    }
    assert {link["task_id"] for link in by_task["task_links"]} == {task.id}

    # spec -> tasks: every item and the top-level projection expose its task links.
    by_spec = asyncio.run(router.execute_tool(
        "spec_get", {"ids": ["spec-implements"]}, "session-1"
    ))
    assert [link["task_id"] for link in by_spec["task_links"]] == [task.id, later_task.id]
    assert by_spec["items"][0]["task_links"] == by_spec["task_links"]
    assert {link["relation"] for link in by_spec["task_links"]} == {"implements"}
    assert by_spec["task_links"][0]["created_by"] == "@planner"


def test_spec_write_batch_relations_get_cluster_and_filters(db_session):
    from app.db.models import Project

    db_session.add(Project(id="spec-project", name="Spec project"))
    db_session.commit()
    router = CommandRouter(db_session)

    result = asyncio.run(router.execute_tool("spec_write", {
        "ops": [
            {
                "op": "create", "id": "spec-a", "project_id": "spec-project",
                "kind": "decision", "title": "Use MCP", "body": "MCP is the surface",
                "derived_from_sha": "abc123", "derived_by": "codex", "confidence": "derived",
            },
            {
                "op": "create", "id": "spec-b", "project_id": "spec-project",
                "kind": "constraint", "title": "No direct DB", "body": "Use tools only",
            },
            {"op": "relation", "from_id": "spec-a", "to_id": "spec-b", "kind": "depends_on"},
            {
                "op": "anchor", "spec_item_id": "spec-b", "repo": "/tmp/spec-project",
                "path": "backend/app/mcp.py", "symbol": "MCPServer",
                "relation": "constrains", "anchor_sha": "a" * 64,
            },
        ],
    }, "session-1"))

    assert result["action"] == "spec_written"
    assert result["count"] == 4
    assert {item["id"] for item in result["items"]} == {"spec-a", "spec-b"}
    assert result["relations"] == [{"from_id": "spec-a", "to_id": "spec-b", "kind": "depends_on"}]

    fetched = asyncio.run(router.execute_tool("spec_get", {"ids": ["spec-a"]}, "session-1"))
    assert {item["id"] for item in fetched["items"]} == {"spec-a", "spec-b"}
    assert fetched["relations"] == result["relations"]
    assert fetched["items"][0]["relations"] == result["relations"]
    assert fetched["anchors"] == result["anchors"]
    anchored = next(item for item in fetched["items"] if item["id"] == "spec-b")
    assert anchored["anchors"] == result["anchors"]

    filtered = asyncio.run(router.execute_tool(
        "spec_get", {"filter": {"project_id": "spec-project", "kind": "decision"}}, "session-1"
    ))
    assert [item["id"] for item in filtered["items"]] == ["spec-a"]

    db_session.get(SpecItem, "spec-a").archive()
    db_session.commit()
    assert asyncio.run(router.execute_tool("spec_get", {"ids": ["spec-a"]}, "session-1"))["items"] == []


def test_spec_supersede_and_batch_is_atomic(db_session):
    from app.db.models import Project

    db_session.add(Project(id="spec-project", name="Spec project"))
    db_session.commit()
    router = CommandRouter(db_session)

    asyncio.run(router.execute_tool("spec_write", {
        "ops": [{
            "op": "create", "id": "old-spec", "project_id": "spec-project",
            "kind": "requirement", "title": "Old", "body": "old body",
        }],
    }, "session-1"))
    superseded = asyncio.run(router.execute_tool("spec_write", {
        "ops": [{"op": "supersede", "id": "old-spec", "new_id": "new-spec", "body": "new body"}],
    }, "session-1"))
    assert {item["id"] for item in superseded["items"]} == {"old-spec", "new-spec"}
    assert next(item for item in superseded["items"] if item["id"] == "old-spec")["status"] == "superseded"
    assert next(item for item in superseded["items"] if item["id"] == "new-spec")["supersedes_id"] == "old-spec"

    failed = asyncio.run(router.execute_tool("spec_write", {
        "ops": [
            {"op": "create", "id": "rolled-back", "project_id": "spec-project", "kind": "design", "title": "x", "body": "x"},
            {"op": "not-an-op"},
        ],
    }, "session-1"))
    assert "one of create" in failed["error"]
    assert db_session.query(SpecItem).filter(SpecItem.id == "rolled-back").count() == 0
    assert db_session.query(SpecRelation).count() == 0


# --- realization: derived agreed/built projection (CTV2-1395) --------------


def _write_module(repo_root: str, body: str) -> None:
    (Path(repo_root) / "mod.py").write_text(body)


def _commit(repo_root: str, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=repo_root, check=True, capture_output=True
    )
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return proc.stdout.strip()


def test_realization_defaults_to_agreed_and_db_check_rejects_bad_value(db_session):
    db_session.add(Project(id="real-proj", name="Realization project"))
    db_session.commit()
    write_specs(db_session, [{
        "op": "create", "id": "real-item", "project_id": "real-proj",
        "kind": "design", "title": "x", "body": "x",
    }])
    item = db_session.get(SpecItem, "real-item")
    assert item.realization == "agreed"

    item.realization = "bogus"
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_realization_agreed_when_no_implements_anchor(db_session):
    db_session.add(Project(id="real-proj-2", name="P"))
    db_session.commit()
    result = write_specs(db_session, [{
        "op": "create", "id": "no-anchor-item", "project_id": "real-proj-2",
        "kind": "design", "title": "x", "body": "x", "status": "active",
    }])
    realization = result["items"][0]["realization"]
    assert realization["state"] == "agreed"
    assert "implements" in realization["why"]
    assert realization["next"]


def test_realization_agreed_when_anchor_does_not_resolve(db_session, git_repo_root):
    db_session.add(Project(id="real-proj-3", name="P", repo_root=git_repo_root))
    db_session.commit()
    _write_module(git_repo_root, "def foo():\n    return 1\n")
    _commit(git_repo_root, "add mod")
    write_specs(db_session, [
        {
            "op": "create", "id": "unresolved-item", "project_id": "real-proj-3",
            "kind": "design", "title": "x", "body": "x", "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "unresolved-item", "repo": "real-proj-3",
            "path": "mod.py", "symbol": "foo", "relation": "implements",
        },
    ])
    # foo vanishes from the working tree after the anchor was written.
    _write_module(git_repo_root, "def bar():\n    return 1\n")
    _commit(git_repo_root, "remove foo")

    fetched = get_specs(db_session, ids=["unresolved-item"])
    realization = fetched["items"][0]["realization"]
    assert realization["state"] == "agreed"
    assert "không giải được" in realization["why"]


def test_realization_agreed_when_anchor_resolves_but_task_not_done(db_session, git_repo_root):
    project = Project(id="real-proj-4", name="P", repo_root=git_repo_root)
    task = Task(id="REAL-1", project=project.id, title="Implement it", status="todo")
    db_session.add_all([project, task])
    db_session.commit()
    _write_module(git_repo_root, "def foo():\n    return 1\n")
    _commit(git_repo_root, "add mod")
    write_specs(db_session, [
        {
            "op": "create", "id": "todo-task-item", "project_id": "real-proj-4",
            "kind": "design", "title": "x", "body": "x", "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "todo-task-item", "repo": "real-proj-4",
            "path": "mod.py", "symbol": "foo", "relation": "implements",
        },
        {
            "op": "task_link", "spec_item_id": "todo-task-item", "task_id": "REAL-1",
            "relation": "implements", "confidence": "asserted", "created_by": "@executor",
        },
    ])
    fetched = get_specs(db_session, ids=["todo-task-item"])
    realization = fetched["items"][0]["realization"]
    assert realization["state"] == "agreed"
    assert "done" in realization["why"]


def test_realization_built_requires_all_three_conditions(db_session, git_repo_root):
    project = Project(id="real-proj-5", name="P", repo_root=git_repo_root)
    task = Task(
        id="REAL-2", project=project.id, title="Implement it", status="done",
        executor="@executor", reviewer="@reviewer", result_ref="base..head",
    )
    db_session.add_all([project, task])
    db_session.commit()
    _write_module(git_repo_root, "def foo():\n    return 1\n")
    _commit(git_repo_root, "add mod")
    write_specs(db_session, [
        {
            "op": "create", "id": "built-item", "project_id": "real-proj-5",
            "kind": "design", "title": "x", "body": "x", "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "built-item", "repo": "real-proj-5",
            "path": "mod.py", "symbol": "foo", "relation": "implements",
        },
        {
            "op": "task_link", "spec_item_id": "built-item", "task_id": "REAL-2",
            "relation": "implements", "confidence": "asserted", "created_by": "@executor",
        },
    ])
    fetched = get_specs(db_session, ids=["built-item"])
    realization = fetched["items"][0]["realization"]
    assert realization["state"] == "built"
    assert realization["why"]
    assert realization["next"] is None


def test_spec_write_rejects_realization_top_level(db_session):
    db_session.add(Project(id="reject-proj", name="P"))
    db_session.commit()
    with pytest.raises(SpecError, match="realization"):
        write_specs(db_session, [{
            "op": "create", "id": "reject-item", "project_id": "reject-proj",
            "kind": "design", "title": "x", "body": "x", "realization": "built",
        }])
    assert db_session.query(SpecItem).filter(SpecItem.id == "reject-item").count() == 0


def test_spec_write_rejects_realization_nested_in_patch(db_session):
    db_session.add(Project(id="reject-proj-2", name="P"))
    db_session.commit()
    write_specs(db_session, [{
        "op": "create", "id": "reject-item-2", "project_id": "reject-proj-2",
        "kind": "design", "title": "x", "body": "x",
    }])
    with pytest.raises(SpecError, match="realization"):
        write_specs(db_session, [{
            "op": "update", "id": "reject-item-2",
            "patch": {"realization": "built"},
        }])
    item = db_session.get(SpecItem, "reject-item-2")
    assert item.realization == "agreed"


def test_spec_write_rejects_realization_in_supersede(db_session):
    db_session.add(Project(id="reject-proj-3", name="P"))
    db_session.commit()
    write_specs(db_session, [{
        "op": "create", "id": "reject-item-3", "project_id": "reject-proj-3",
        "kind": "design", "title": "x", "body": "x",
    }])
    with pytest.raises(SpecError, match="realization"):
        write_specs(db_session, [{
            "op": "supersede", "old_id": "reject-item-3", "new_id": "reject-item-3-v2",
            "item": {"realization": "built"},
        }])
    assert db_session.query(SpecItem).filter(SpecItem.id == "reject-item-3-v2").count() == 0
    assert db_session.get(SpecItem, "reject-item-3").status == "draft"


def test_spec_write_via_router_rejects_realization(db_session):
    db_session.add(Project(id="reject-proj-4", name="P"))
    db_session.commit()
    router = CommandRouter(db_session)
    result = asyncio.run(router.execute_tool("spec_write", {"ops": [{
        "op": "create", "id": "reject-item-4", "project_id": "reject-proj-4",
        "kind": "design", "title": "x", "body": "x", "realization": "built",
    }]}, "session-1"))
    assert "realization" in result["error"]
    assert db_session.query(SpecItem).filter(SpecItem.id == "reject-item-4").count() == 0


def test_spec_get_backlog_filter_is_active_and_not_built(db_session, git_repo_root):
    project = Project(id="backlog-proj", name="P", repo_root=git_repo_root)
    done_task = Task(
        id="BACKLOG-1", project=project.id, title="Done", status="done",
        executor="@executor", reviewer="@reviewer", result_ref="base..head",
    )
    db_session.add_all([project, done_task])
    db_session.commit()
    _write_module(git_repo_root, "def foo():\n    return 1\n")
    _commit(git_repo_root, "add mod")

    write_specs(db_session, [
        # built: active, anchored, task done -- must NOT show up in backlog
        {
            "op": "create", "id": "backlog-built", "project_id": "backlog-proj",
            "kind": "design", "title": "built", "body": "x", "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "backlog-built", "repo": "backlog-proj",
            "path": "mod.py", "symbol": "foo", "relation": "implements",
        },
        {
            "op": "task_link", "spec_item_id": "backlog-built", "task_id": "BACKLOG-1",
            "relation": "implements", "confidence": "asserted", "created_by": "@executor",
        },
        # agreed and active: no anchor -- must show up in backlog
        {
            "op": "create", "id": "backlog-agreed", "project_id": "backlog-proj",
            "kind": "design", "title": "agreed", "body": "x", "status": "active",
        },
        # agreed but draft, not active -- must NOT show up in backlog
        {
            "op": "create", "id": "backlog-draft", "project_id": "backlog-proj",
            "kind": "design", "title": "draft", "body": "x", "status": "draft",
        },
    ])

    backlog = get_specs(db_session, filters={"project_id": "backlog-proj", "backlog": True})
    ids = {item["id"] for item in backlog["items"]}
    assert ids == {"backlog-agreed"}
    for item in backlog["items"]:
        assert item["realization"]["state"] != "built"
        assert item["status"] == "active"


def test_spec_get_realization_filter_selects_state(db_session, git_repo_root):
    project = Project(id="filt-proj", name="P", repo_root=git_repo_root)
    task = Task(
        id="FILT-1", project=project.id, title="Done", status="done",
        executor="@executor", reviewer="@reviewer", result_ref="base..head",
    )
    db_session.add_all([project, task])
    db_session.commit()
    _write_module(git_repo_root, "def foo():\n    return 1\n")
    _commit(git_repo_root, "add mod")
    write_specs(db_session, [
        {
            "op": "create", "id": "filt-built", "project_id": "filt-proj",
            "kind": "design", "title": "x", "body": "x", "status": "active",
        },
        {
            "op": "anchor", "spec_item_id": "filt-built", "repo": "filt-proj",
            "path": "mod.py", "symbol": "foo", "relation": "implements",
        },
        {
            "op": "task_link", "spec_item_id": "filt-built", "task_id": "FILT-1",
            "relation": "implements", "confidence": "asserted", "created_by": "@executor",
        },
        {
            "op": "create", "id": "filt-agreed", "project_id": "filt-proj",
            "kind": "design", "title": "y", "body": "y", "status": "active",
        },
    ])
    built = get_specs(db_session, filters={"project_id": "filt-proj", "realization": "built"})
    assert {item["id"] for item in built["items"]} == {"filt-built"}

    agreed = get_specs(db_session, filters={"project_id": "filt-proj", "realization": "agreed"})
    assert {item["id"] for item in agreed["items"]} == {"filt-agreed"}


def test_spec_get_realization_filter_rejects_bad_value(db_session):
    db_session.add(Project(id="bad-filt-proj", name="P"))
    db_session.commit()
    with pytest.raises(SpecError, match="realization filter"):
        get_specs(db_session, filters={"project_id": "bad-filt-proj", "realization": "done"})
