import pytest

from app.db.models import AgentNote, Project, Task
from app.services.command_router import CommandRouter
from app.services.tool_registry import TOOL_REGISTRY


@pytest.mark.asyncio
async def test_manage_notes_save_link_list_search_archive(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.command_router.embed_text",
        lambda text, db: [0.1, 0.2, 0.3],
    )
    db_session.add(Project(id="proj-notes", name="Notes project"))
    db_session.add(Task(id="TASK-NOTE", project="proj-notes", title="Note task"))
    db_session.commit()
    router = CommandRouter(db_session)

    saved = await router.execute_tool(
        "manage_notes",
        {
            "action": "save",
            "title": "Deploy lesson",
            "content": "Use a transactional outbox for worker dispatch.",
            "note_type": "observation",
            "project_id": "proj-notes",
        },
        "session-1",
    )
    assert saved["action"] == "note_saved"
    note_id = saved["id"]
    assert db_session.get(AgentNote, note_id).projects[0].id == "proj-notes"

    linked = await router.execute_tool(
        "manage_notes", {"action": "link", "id": note_id, "task_id": "TASK-NOTE"}, "session-1"
    )
    assert linked["action"] == "note_linked"
    listed = await router.execute_tool("manage_notes", {"action": "list", "project_id": "proj-notes"}, "session-1")
    assert listed["notes"][0]["id"] == note_id
    searched = await router.execute_tool("manage_notes", {"action": "search", "query": "outbox"}, "session-1")
    assert searched["notes"][0]["id"] == note_id

    archived = await router.execute_tool("manage_notes", {"action": "archive", "id": note_id}, "session-1")
    assert archived["action"] == "note_archived"
    assert (await router.execute_tool("manage_notes", {"action": "list"}, "session-1"))["notes"] == []


def test_manage_notes_is_registered_with_research_group():
    spec = TOOL_REGISTRY["manage_notes"]
    assert spec.handler == "manage_notes"
    assert spec.group == "research"
