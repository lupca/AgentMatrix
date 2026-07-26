import pytest
from app.services.command_router import CommandRouter

def test_parse_pm_command():
    router = CommandRouter(None)
    cmd, args = router.parse('/pm Add feature')
    assert cmd == 'create_task'
    assert args == 'Add feature'

def test_parse_regular_message():
    router = CommandRouter(None)
    cmd, args = router.parse('hello world')
    assert cmd is None
    assert args == 'hello world'
