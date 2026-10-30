"""Small persistence facts preserve discovery authority without a catalog chain."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

from backend.app.agent_runtime import CapabilityRegistry, ToolDefinition, AgentToolResult, ToolContext, CancellationToken
from backend.app.agent_runtime.types import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


class Tool:
    def __init__(self, name='archive.read', **kwargs):
        self.definition = ToolDefinition(name, 'Read archive', {'type': 'object'}, exposure='deferred', **kwargs)
    async def execute(self, arguments, context, on_update=None):
        return AgentToolResult('ok')


def resolve(tools=None, **kwargs):
    return CapabilityRegistry(tools or [Tool()]).resolve(role_id='r', session_key='role:r', run_id=kwargs.pop('run_id', 'run'),
        discovery_enabled=True, **kwargs)


def test_window_restores_ordered_names_using_current_definition():
    history = [ToolResultMessage('search', 'tool_search', '{}', unlocked_tools=('second', 'first'))]
    restored = resolve([Tool('first', version='2'), Tool('second')]).select_window(history)
    assert [item.name for item in restored.snapshot.tools] == ['tool_search', 'second', 'first']
    assert restored.definition('first').version == '2'
    assert resolve([Tool('first')]).select_window([]).get('first') is None

def test_call_proof_is_one_definition_and_cannot_grow_with_unused_catalog():
    one = resolve(activated_tools={'archive.read'})
    many = resolve([Tool(), *(Tool(f'unused_{i}') for i in range(200))], activated_tools={'archive.read'})
    assert one.call_record('archive.read') == many.call_record('archive.read')
    proof = one.call_record('archive.read')
    assert proof['format'] == 'tool-call-v1'
    assert [item['name'] for item in proof['tools']] == ['archive.read']
    assert 'candidates' not in proof and 'policyDecisions' not in proof


def test_search_uses_current_run_view_without_request_version_gate():
    async def run():
        initial = resolve()
        context = ToolContext('r', 'role:r', 'run', CancellationToken(), 'search', initial.snapshot.snapshot_id, initial)
        await initial.get('tool_search').execute({'query': 'select:archive.read'}, context)
        initial.publish_revision(initial.pending_revision('search'))
        assert initial.current().get('archive.read') is not None
        result = await initial.get('tool_search').execute({'query': 'select:archive.read'}, replace(context, tool_call_id='again'))
        assert result.details['already_loaded'] == ['archive.read']
    asyncio.run(run())

def test_legacy_selection_is_readable_history_but_does_not_restore_tools(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    legacy = {'format': 'tool-selection-v1', 'roleId': 'r', 'sessionKey': 'role:r',
              'selected': [{'name': 'archive.read', 'source': 'builtin', 'fingerprint': 'old'}]}
    run = AgentRun(runId='old', roleId='r', sessionKey='role:r', status='created', startedAt=datetime.now(timezone.utc),
        modelSnapshot={'toolSelection': legacy})
    sessions.create_run_with_messages(run, 'old request')
    sessions.update_run('old', status='running')
    sessions.update_run('old', status='completed')
    restarted = SessionStore(sessions.database_path)
    assert restarted.get_run('old').modelSnapshot['toolSelection'] == legacy
    assert resolve().select_window(restarted.runtime_messages('role:r')).get('archive.read') is None

def test_used_definition_audit_is_immutable_and_has_legacy_reader(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.create_run(AgentRun(runId='run', roleId='r', sessionKey='role:r', status='created', startedAt=datetime.now(timezone.utc)))
    proof = resolve(activated_tools={'archive.read'}).call_record('archive.read')
    kwargs = dict(run_id='run', role_id='r', tool_name='archive.read', call_id='call', argument_summary={}, capability_binding=proof)
    sessions.start_tool_audit(**kwargs)
    assert sessions.call_binding('run', 'call') == proof
    sessions.finish_tool_audit(run_id='run', call_id='call', result_category='uncertain')
    import pytest
    with pytest.raises(ValueError, match='open audit'):
        sessions.bind_tool_audit(run_id='run', role_id='r', call_id='call', snapshot_id='new',
            tool_source='builtin', tool_version='new', policy_decision='allowed', capability_binding=proof)
    try:
        sessions.start_tool_audit(**kwargs)
    except ValueError:
        pass
    else:
        raise AssertionError('a repeated host ID must not erase/replay the prior audit')
    assert sessions.list_tool_audits('run')[0]['resultCategory'] == 'uncertain'
    assert 'capabilityRevisions' not in sessions.get_run('run').modelSnapshot


def test_call_audit_preserves_old_facts_after_current_definition_changes():
    tool = Tool()
    resolution = resolve([tool], activated_tools={'archive.read'})
    proof = resolution.call_record('archive.read')
    tool.definition = replace(tool.definition, risk='mutating', version='2')
    context = ToolContext('r', 'role:r', 'run', CancellationToken())
    assert resolution.authorization_denial(resolution.definition('archive.read'), context) is None
    assert proof['tools'][0]['risk'] == 'read_only'
    assert resolution.call_record('archive.read')['tools'][0]['version'] == '2'

def test_clear_history_and_different_thread_have_no_cached_unlocks(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.open_role_session('r')
    sessions.append_message('role:r', 'user', 'discover')
    sessions.append_agent_message('role:r', AssistantMessage(tool_calls=(ToolCall('search', 'tool_search', {'query': 'archive'}),)))
    sessions.append_agent_message('role:r', ToolResultMessage('search', 'tool_search', '{}', unlocked_tools=('archive.read',)))
    assert resolve().select_window(sessions.runtime_messages('role:r')).get('archive.read') is not None
    assert resolve().select_window(sessions.runtime_messages('thread:other')).get('archive.read') is None
    sessions.delete_role_session('r')
    assert resolve().select_window(sessions.runtime_messages('role:r')).get('archive.read') is None

def test_bridge_reads_old_and_new_call_facts_without_rewriting_history(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    binding = resolve(activated_tools={'archive.read'})
    sessions.create_run_with_messages(AgentRun(runId='run', roleId='r', sessionKey='role:r', status='created',
        startedAt=datetime.now(timezone.utc)), 'discover')
    legacy = binding.snapshot.to_dict(include_prompt_sections=False)
    # Historical fixture, not a remaining production writer for the removed chain.
    with sqlite3.connect(sessions.database_path) as db:
        db.execute('UPDATE agent_runs SET model_snapshot_json=? WHERE run_id=?',
            (json.dumps({'capabilityRevisions': [legacy], 'capabilityCalls': {'old': legacy['snapshotId']}}), 'run'))
    sessions.start_tool_audit(run_id='run', role_id='r', tool_name='archive.read', call_id='new', argument_summary={},
        capability_binding=binding.call_record('archive.read'))
    assert sessions.call_binding('run', 'old') == legacy
    assert sessions.call_binding('run', 'new')['tools'] == [item for item in legacy['tools'] if item['name'] == 'archive.read']
    sessions.finish_tool_audit(run_id='run', call_id='new', result_category='uncertain')
    # Bridge restart only marks a control interruption; known domain result stays unchanged.
    restarted = SessionStore(tmp_path / 'sessions.db')
    assert restarted.list_tool_audits('run')[0]['resultCategory'] == 'uncertain'
    assert restarted.get_run('run').modelSnapshot['capabilityRevisions'] == [legacy]


def test_author_proof_requires_an_active_exact_call_and_builtin_skill(tmp_path):
    from types import SimpleNamespace
    from backend.app.proactive_runtime.drift_skills import DriftSkillCatalog, BUILTIN_ROOT
    from backend.app.agent_runtime.skills import SkillRegistry
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.create_run_with_messages(AgentRun(runId='run', roleId='r', sessionKey='role:r', status='created',
        startedAt=datetime.now(timezone.utc)), 'write a Drift skill')
    sessions.update_run('run', status='running')
    scope = SimpleNamespace(enabled=True, scopeId='scope', revision=3)
    catalog = DriftSkillCatalog.__new__(DriftSkillCatalog)
    catalog.app = SimpleNamespace(sessions=sessions)
    catalog.configuration = lambda role: SimpleNamespace(driftAuthoring=scope)
    author = next(skill.materialize() for skill in SkillRegistry.discover(BUILTIN_ROOT, source='builtin').skills
        if skill.skill_id == 'create-drift-skill')
    binding = resolve([Tool('write_file')], activated_tools={'write_file'}).call_record('write_file')
    binding['skills'] = [{'id': author.skill_id, 'source': 'builtin', 'contentHash': author.content_hash}]
    def insert(call, proof):
        sessions.start_tool_audit(run_id='run', role_id='r', tool_name='write_file', call_id=call,
            argument_summary={}, capability_binding=proof)
        return ToolContext('r', 'role:r', 'run', CancellationToken(), call, 'cap-run')
    good = insert('good', binding)
    evidence = catalog._writer('r', scope, good, lambda context: True)
    assert evidence[1]['format'] == 'drift-author-v1'
    assert not {'tools', 'candidates', 'skills'} & evidence[1].keys()
    for call, change in [('inactive', {'active': False}), ('forged', {'skills': [{**binding['skills'][0], 'source': 'role'}]}),
                         ('changed', {'skills': [{**binding['skills'][0], 'contentHash': '0' * 64}]})]:
        assert catalog._writer('r', scope, insert(call, {**binding, **change}), lambda context: True) is None
    assert catalog._writer('r', scope, replace(good, tool_call_id='missing'), lambda context: True) is None
    assert catalog._writer('r', scope, replace(good, capability_snapshot_id='other'), lambda context: True) is None
    assert catalog._writer('r', scope, good, lambda context: False) is None
    sessions.finish_tool_audit(run_id='run', call_id='good', result_category='succeeded')
    assert catalog._writer('r', scope, good, lambda context: True) is None


def test_window_restoration_remeasures_current_schema_budget_and_filters_absent_names():
    tools = [Tool(f'large_{i}') for i in range(3)]
    for tool in tools:
        tool.definition = replace(tool.definition, description='x' * 50000)
    history = [ToolResultMessage('search', 'tool_search', '{}', unlocked_tools=tuple(tool.definition.name for tool in tools))]
    restored = resolve(tools).select_window(history)
    assert len([item for item in restored.snapshot.tools if item.exposure == 'deferred']) == 2
    assert [item.name for item in resolve([Tool('other')]).select_window(history).snapshot.tools] == ['tool_search']

def test_spawn_parent_read_and_web_resolve_current_implementation(tmp_path):
    from backend.app.agent_runtime.spawn_tool import _ChildRead, _ChildTool
    parent = Tool('read_file')
    registry = CapabilityRegistry([parent])
    capabilities = registry.resolve(role_id='r', session_key='role:r', run_id='run', activated_tools={'read_file'})
    current = lambda: capabilities.get('read_file')
    read = _ChildRead(parent, Tool('task_read'), tmp_path, current)
    web = _ChildTool(parent, parent.definition, lambda _: True, current)
    replacement = Tool('read_file')
    async def execute(*args):
        return AgentToolResult('current implementation')
    replacement.execute = execute
    registry.register(replacement)
    context = ToolContext('r', 'role:r', 'run', CancellationToken())
    assert asyncio.run(read.execute({'path': '../parent'}, context)).content == 'current implementation'
    assert asyncio.run(web.execute({}, context)).content == 'current implementation'
    registry.unregister('read_file')
    assert asyncio.run(web.execute({}, context)).is_error

def test_service_activation_is_atomic_when_declarations_exceed_total_budget():
    import pytest
    from backend.app.agent_runtime.discovery import ServiceGrant
    grant = ServiceGrant('grant', 'notes', 'r', frozenset({'read_only'}))
    binding = resolve(service_grants=(grant,))
    remote = Tool('mcp_notes__read', source='mcp:notes')
    remote.definition = replace(remote.definition, description='x' * 140000)
    context = ToolContext('r', 'role:r', 'run', CancellationToken(), 'mount', binding.snapshot.snapshot_id, binding)
    with pytest.raises(ValueError, match='schema budget'):
        binding.stage_service_tools(context, grant_id='grant', parent_snapshot_id=binding.snapshot.snapshot_id,
            tools=(remote,), activate=True)
    assert binding.pending_revision('mount') is None
    assert binding.current().provider_schemas() == binding.provider_schemas()


def test_service_reconnect_changes_current_implementation_and_schema():
    from backend.app.agent_runtime.discovery import ServiceGrant
    grant = ServiceGrant('grant', 'notes', 'r', frozenset({'read_only'}))
    initial = resolve(service_grants=(grant,))
    old = Tool('mcp_notes__read', source='mcp:notes')
    context = ToolContext('r', 'role:r', 'run', CancellationToken(), 'add', initial.snapshot.snapshot_id, initial)
    initial.stage_service_tools(context, grant_id='grant', parent_snapshot_id=initial.snapshot.snapshot_id, tools=(old,), activate=True)
    initial.publish_revision(initial.pending_revision('add'))
    new = Tool('mcp_notes__read', source='mcp:notes', version='2')
    new.definition = replace(new.definition, input_schema={'type': 'object', 'required': ['query']})
    reconnect = replace(context, tool_call_id='reconnect')
    current = initial.current()
    current.stage_service_tools(reconnect, grant_id='grant', parent_snapshot_id=current.snapshot.snapshot_id, tools=(new,), rebind=True)
    current.publish_revision(current.pending_revision('reconnect'))
    assert initial.current().get(new.definition.name) is new
    assert initial.current().definition(new.definition.name).input_schema['required'] == ['query']

def test_namespace_metadata_change_keeps_current_window_name_loaded():
    changed = Tool(namespace_summary='current archive')
    history = [ToolResultMessage('search', 'tool_search', '{}', unlocked_tools=('archive.read',))]
    restored = resolve([changed]).select_window(history)
    assert restored.definition('archive.read').namespace_summary == 'current archive'
