import asyncio

from sqlalchemy import inspect

from app.db.models import SpecItem, SpecRelation
from app.services.command_router import CommandRouter


def test_living_spec_tables_are_registered(db_session):
    inspector = inspect(db_session.bind)
    tables = inspector.get_table_names()
    assert "spec_item" in tables
    assert "spec_relation" in tables
    assert {
        "id", "project_id", "kind", "title", "body", "status", "supersedes_id",
        "source_doc_id", "derived_from_sha", "derived_by", "confidence", "verified_at",
        "verified_by", "embedding", "archived_at",
    } <= {column["name"] for column in inspector.get_columns("spec_item")}
    assert {"from_id", "to_id", "kind"} <= {
        column["name"] for column in inspector.get_columns("spec_relation")
    }


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
        ],
    }, "session-1"))

    assert result["action"] == "spec_written"
    assert result["count"] == 3
    assert {item["id"] for item in result["items"]} == {"spec-a", "spec-b"}
    assert result["relations"] == [{"from_id": "spec-a", "to_id": "spec-b", "kind": "depends_on"}]

    fetched = asyncio.run(router.execute_tool("spec_get", {"ids": ["spec-a"]}, "session-1"))
    assert {item["id"] for item in fetched["items"]} == {"spec-a", "spec-b"}
    assert fetched["relations"] == result["relations"]
    assert fetched["items"][0]["relations"] == result["relations"]

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
