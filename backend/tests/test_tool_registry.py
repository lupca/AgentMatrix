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


def test_registry_has_tools_with_unique_names():
    assert len(TOOL_REGISTRY) == 36
    assert list(TOOL_REGISTRY) == [
        'create_task',
        'get_status',
        'manage_inbox',
        'ask_human',
        'get_run_output',
        'get_stats',
        'query_db',
        'dispatch_task',
        'record_verdict',
        'attach_result',
        'approve_gate',
        'land_task',
        'cancel_task',
        'reopen_task',
        'get_task_events',
        'wait_for_task',
        'archive_task',
        'suggest_agents',
        'request_review',
        'generate_spec_plan',
        'critique_spec_plan',
        'compact_context',
        'manage_project',
        'manage_agent',
        'manage_knowledge',
        'manage_notes',
        'update_settings',
        'update_task',
        'get_minimal_context',
        'get_impact_radius',
        'save_project_context',
        'impl_design',
        'spec_write',
        'spec_get',
        'spec_stale',
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

    assert names == {'create_task', 'manage_inbox', 'get_status', 'get_run_output', 'get_stats', 'query_db', 'load_tools'}
    assert 'pm_create_task' not in names
    assert 'dispatch_task' not in names
    assert not any('defer_loading' in t for t in tools)


def test_get_group_tool_definitions_returns_deferred_tools_by_group():
    task_lifecycle = get_group_tool_definitions('task_lifecycle')
    assert {t['name'] for t in task_lifecycle} == {
        'dispatch_task',
        'record_verdict',
        'attach_result',
        'approve_gate',
        'cancel_task',
        'reopen_task',
        'archive_task',
        'update_task',
        'request_review',
        'generate_spec_plan',
        'critique_spec_plan',
        'save_project_context',
        'land_task',
        'ask_human',
    }

    query = get_group_tool_definitions('query')
    assert {t['name'] for t in query} == {
        'get_task_events',
        'wait_for_task',
        'suggest_agents',
    }

    session = get_group_tool_definitions('session')
    assert {t['name'] for t in session} == {'compact_context'}

    admin = get_group_tool_definitions('admin')
    assert {t['name'] for t in admin} == {
        'manage_project',
        'manage_agent',
        'manage_knowledge',
        'update_settings',
    }

    research = get_group_tool_definitions('research')
    assert {t['name'] for t in research} == {
        'get_minimal_context',
        'get_impact_radius',
        'manage_notes',
    }

    spec = get_group_tool_definitions('spec')
    assert {t['name'] for t in spec} == {'impl_design', 'spec_write', 'spec_get', 'spec_stale'}

    assert get_group_tool_definitions('nonexistent') is None


def test_research_tools_are_read_only_deferred():
    for name in ('get_minimal_context', 'get_impact_radius'):
        spec = TOOL_REGISTRY[name]
        assert spec.tier == 'deferred'
        assert spec.permission == 'read'
        assert spec.group == 'research'


def test_load_tools_schema_lists_research_group():
    load_tools_spec = TOOL_REGISTRY['load_tools']
    assert 'research' in load_tools_spec.parameters['properties']['group']['enum']
    assert 'spec' in load_tools_spec.parameters['properties']['group']['enum']


def test_spec_tools_are_executor_only():
    assert TOOL_REGISTRY['spec_write'].required_role == 'executor'
    assert TOOL_REGISTRY['spec_get'].required_role == 'executor'
    assert TOOL_REGISTRY['spec_stale'].required_role == 'executor'


def test_update_settings_description_documents_autonomy_and_rejects_default_mode():
    """CTV2-222: the generated ToolSpec must tell coordinators that autonomy
    is the real mode knob and that default_mode is not writable."""
    spec = TOOL_REGISTRY['update_settings']
    assert 'autonomy' in spec.description
    assert 'default_mode' in spec.description
    assert 'supervised' in spec.description
    assert 'plan-only' in spec.description


def test_query_db_schema_summary_warns_about_append_only_gate_ledger():
    """CTV2-1408: the schema summary is the only place a coordinator reads
    before writing gate SQL, so the append-only trap has to be stated THERE.
    `WHERE status='pending'` on gate_records returned 650 rows against 8 truly
    open gates on the live DB -- two coordinators lost a day to it."""
    spec = TOOL_REGISTRY['query_db']
    assert 'gate_records' in spec.description
    assert 'APPEND-ONLY' in spec.description
    assert 'parent_id' in spec.description
    # ...and the summary must point at the view that answers the question,
    # not just warn about the query that does not.
    assert 'open_gates' in spec.description
    assert 'moot' in spec.description


def test_attach_result_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['attach_result']
    # (a) caller-situation trigger
    assert 'result_ref' in spec.description
    # (b) named confusable tool + distinction
    assert 'land_task' in spec.description
    assert 'merge' in spec.description
    # (c) precondition: status
    assert 'dispatched' in spec.description
    # (d) recovery/rejection path
    assert 'reopen_task' in spec.description
    assert 'failed' in spec.description
    # `option` is a single-value enum (only 'request_review'). It cannot be
    # removed here: backend/tests/test_attach_result.py asserts it stays in
    # the schema. Since it must stay, its own description has to explain why
    # the sole value exists rather than leaving it as an unexplained dead
    # choice.
    assert 'option' in spec.parameters['properties']
    assert spec.parameters['properties']['option']['enum'] == ['request_review']
    option_desc = spec.parameters['properties']['option']['description']
    assert 'no other value' in option_desc or 'always routes' in option_desc


def test_reopen_task_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['reopen_task']
    assert 'failed' in spec.description
    assert 'cancel_task' in spec.description
    assert 'awaiting-review' in spec.description
    assert 'todo' in spec.description
    assert 'get_status' in spec.description


def test_approve_gate_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['approve_gate']
    assert 'pending' in spec.description
    assert 'record_verdict' in spec.description
    assert 'gate_record_id' in spec.description
    assert 'dispatch_task' in spec.description or 'request_review' in spec.description


def test_record_verdict_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['record_verdict']
    assert 'in-review' in spec.description
    assert 'approve_gate' in spec.description
    assert 'request_review' in spec.description


def test_land_task_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['land_task']
    assert 'pass verdict' in spec.description
    assert 'attach_result' in spec.description
    assert 'record_verdict' in spec.description
    assert 'landing_failed' in spec.description


def test_critique_spec_plan_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['critique_spec_plan']
    assert 'generate_spec_plan' in spec.description
    assert 'planner' in spec.description
    assert 'plan' in spec.description


def test_generate_spec_plan_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['generate_spec_plan']
    assert 'critique_spec_plan' in spec.description
    assert 'todo' in spec.description
    assert 'critic' in spec.description


def test_manage_inbox_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['manage_inbox']
    assert 'create_task' in spec.description
    assert 'promote' in spec.description
    assert 'list' in spec.description


def test_spec_write_description_has_situation_confusable_precondition_recovery():
    spec = TOOL_REGISTRY['spec_write']
    assert 'impl_design' in spec.description
    assert 'spec_get' in spec.description
    assert 'derived_from_sha' in spec.description


def test_all_descriptions_carry_situation_confusable_precondition_recovery_signal():
    """Every one of the 35 tools must convey: (a) a caller-situation trigger,
    (b) a named confusable other tool + how this one differs, (c) a
    precondition (status/field), and (d) a recovery/rejection path -- without
    a rigid WHEN:/NOT:/PRECONDITION:/REJECTION: label format."""
    tool_names = set(TOOL_REGISTRY)
    forbidden_labels = ('WHEN:', 'NOT:', 'PRECONDITION:', 'REJECTION:')

    for name, spec in TOOL_REGISTRY.items():
        desc = spec.description
        for label in forbidden_labels:
            assert label not in desc, f'{name} description uses a forbidden rigid label {label!r}'

        # (b) a named confusable other tool: at least one other registry tool
        # name appears as a substring, referenced by name.
        other_names_mentioned = [
            other for other in tool_names
            if other != name and other in desc
        ]
        assert other_names_mentioned, f'{name} description names no other tool for contrast'

        # description length is a weak proxy but a one-liner cannot carry
        # situation + distinction + precondition + recovery.
        assert len(desc) > 120, f'{name} description too short to carry the required signal'


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


def test_a_tool_missing_from_canonical_order_is_not_silently_dropped():
    """CTV2-1418: the ordering list must never decide membership.

    The first split (CTV2-1417) built the registry by walking
    `_CANONICAL_ORDER`, so a tool declared in a `*_specs.py` file but forgotten
    there vanished from the MCP surface with no error. Caught on review while
    both lists still matched 36/36 -- a trap armed, not yet sprung.
    """
    from dataclasses import replace

    from app.services import tool_specs

    sample = next(iter(tool_specs._unmerged_specs.values()))
    stray = replace(sample, name="zz_tool_not_in_canonical_order")
    assert stray.name not in tool_specs._CANONICAL_ORDER

    original = dict(tool_specs._unmerged_specs)
    try:
        tool_specs._unmerged_specs[stray.name] = stray
        rebuilt = tool_specs._ordered_specs()
    finally:
        tool_specs._unmerged_specs.clear()
        tool_specs._unmerged_specs.update(original)

    assert stray.name in rebuilt, "tool declared but missing from order was dropped"
    # Strays go last; the canonical order of everything else is untouched.
    assert list(rebuilt)[-1] == stray.name
    assert [n for n in rebuilt if n != stray.name] == list(tool_specs.ALL_TOOL_SPECS)
