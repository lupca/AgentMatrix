import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.services.command_router import COMMANDS, CommandRouter
from app.services.tool_definitions import get_tool_definitions
from app.services.tool_registry import (
    TOOL_REGISTRY,
    dump_registry,
    get_by_slash_alias,
    get_group_tool_definitions,
    resolve_tool_name,
    to_openai_tools,
)


@pytest.fixture
def db_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_registry_has_thirteen_tools_with_unique_names():
    assert len(TOOL_REGISTRY) == 13
    assert list(TOOL_REGISTRY) == [
        'create_task',
        'get_status',
        'query_db',
        'dispatch_task',
        'record_verdict',
        'approve_gate',
        'cancel_task',
        'compact_context',
        'manage_project',
        'manage_agent',
        'manage_knowledge',
        'update_task',
        'load_tools',
    ]
    for name, spec in TOOL_REGISTRY.items():
        assert spec.name == name


def test_registry_specs_have_valid_schema_and_metadata():
    for spec in TOOL_REGISTRY.values():
        assert spec.description
        assert spec.parameters.get('type') == 'object'
        assert 'properties' in spec.parameters
        assert spec.tier in ('eager', 'deferred')
        assert spec.permission in ('read', 'write', 'admin')
        assert spec.entity
        assert spec.group


def test_deprecated_alias_resolves_to_canonical_name():
    assert resolve_tool_name('pm_create_task') == 'create_task'
    assert resolve_tool_name('create_task') == 'create_task'
    assert resolve_tool_name('unknown_tool') == 'unknown_tool'


def test_get_by_slash_alias():
    spec = get_by_slash_alias('/pm')
    assert spec is not None
    assert spec.name == 'create_task'
    assert get_by_slash_alias('/nonexistent') is None


def test_to_openai_tools_projection_shape():
    specs = list(TOOL_REGISTRY.values())
    tools = to_openai_tools(specs)
    assert len(tools) == len(specs)
    for tool, spec in zip(tools, specs):
        assert tool == {
            'name': spec.name,
            'description': spec.description,
            'input_schema': spec.parameters,
        }


def test_get_tool_definitions_is_baseline_eager_set_only():
    tools = get_tool_definitions()
    names = {t['name'] for t in tools}

    assert names == {'create_task', 'get_status', 'query_db', 'load_tools'}
    assert 'pm_create_task' not in names
    assert 'dispatch_task' not in names
    assert not any('defer_loading' in t for t in tools)


def test_get_group_tool_definitions_returns_deferred_tools_by_group():
    task_lifecycle = get_group_tool_definitions('task_lifecycle')
    assert {t['name'] for t in task_lifecycle} == {
        'dispatch_task',
        'record_verdict',
        'approve_gate',
        'cancel_task',
        'update_task',
    }

    session = get_group_tool_definitions('session')
    assert {t['name'] for t in session} == {'compact_context'}

    admin = get_group_tool_definitions('admin')
    assert {t['name'] for t in admin} == {
        'manage_project',
        'manage_agent',
        'manage_knowledge',
    }

    assert get_group_tool_definitions('nonexistent') is None


def test_command_router_commands_derived_from_registry():
    for spec in TOOL_REGISTRY.values():
        if spec.slash_alias is None:
            continue
        assert COMMANDS[spec.slash_alias] == spec.handler
    assert COMMANDS['/help'] == 'show_help'


def test_dump_registry_shape_for_tools_endpoint():
    dump = dump_registry()
    assert len(dump) == len(TOOL_REGISTRY)
    for entry, spec in zip(dump, TOOL_REGISTRY.values()):
        assert entry == {
            'name': spec.name,
            'description': spec.description,
            'slash_alias': spec.slash_alias,
            'tier': spec.tier,
            'group': spec.group,
        }


@pytest.mark.asyncio
async def test_slash_and_tool_call_produce_same_result(db_session):
    from app.db.models import Project

    db_session.add(Project(id='proj-1', name='Test Project'))
    db_session.commit()

    router_slash = CommandRouter(db_session)
    cmd, args = router_slash.parse('/pm Fix the bug --project proj-1')
    slash_result = await router_slash.execute(cmd, args, 'session-1')

    router_tool = CommandRouter(db_session)
    tool_result = await router_tool.execute_tool(
        'create_task',
        {'title': 'Fix the bug', 'project': 'proj-1'},
        'session-2',
    )

    assert slash_result['action'] == tool_result['action'] == 'created'
    assert slash_result['title'] == tool_result['title'] == 'Fix the bug'
    assert slash_result['project'] == tool_result['project'] == 'proj-1'


@pytest.mark.asyncio
async def test_pm_create_task_alias_still_routes_to_create_task(db_session):
    from app.db.models import Project

    db_session.add(Project(id='proj-1', name='Test Project'))
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute_tool(
        'pm_create_task',
        {'title': 'Legacy alias task', 'project': 'proj-1'},
        'session-1',
    )
    assert result['action'] == 'created'
    assert result['title'] == 'Legacy alias task'
