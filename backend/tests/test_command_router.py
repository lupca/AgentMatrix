import pytest
from app.services.command_router import CommandRouter, COMMANDS

def test_command_router_parse():
    router = CommandRouter(None)
    
    # Non-command message
    cmd, args = router.parse("hello world")
    assert cmd is None
    assert args == "hello world"
    
    # Slash command with arguments
    cmd, args = router.parse("/pm create new task")
    assert cmd == "create_task"
    assert args == "create new task"

    # Slash command without arguments
    cmd, args = router.parse("/help")
    assert cmd == "show_help"
    assert args == ""

    # Unknown command
    cmd, args = router.parse("/unknown_cmd arg")
    assert cmd is None
    assert args == "/unknown_cmd arg"

@pytest.mark.asyncio
async def test_command_router_execute():
    router = CommandRouter(None)
    res = await router.execute("show_help", "", "session-1")
    assert "commands" in res
    assert res["commands"] == list(COMMANDS.keys())

    res_unknown = await router.execute("non_existent_command", "", "session-1")
    assert "error" in res_unknown
