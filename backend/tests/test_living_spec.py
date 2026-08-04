import asyncio

from sqlalchemy import inspect

from app.db.models import SpecItem, SpecRelation, SpecTaskLink
from app.services.command_router import CommandRouter


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
