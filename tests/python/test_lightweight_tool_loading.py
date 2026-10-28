"""Small persistence facts preserve discovery authority without a catalog chain."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

from backend.app.agent_runtime import CapabilityRegistry, ToolDefinition, AgentToolResult, ToolContext, CancellationToken
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


def test_cache_contains_only_ordered_selections_and_revalidates_source_and_full_definition():
    first = resolve([Tool('second'), Tool('first')], activated_tools={'first', 'second'}, source_scope='owner-v1')
    cache = first.selection_cache()
    assert cache['format'] == 'tool-selection-v1'
    assert [item['name'] for item in cache['selected']] == ['second', 'first']
    assert all(set(item) == {'name', 'source', 'fingerprint'} for item in cache['selected'])
    restored = resolve([Tool('first'), Tool('second')], source_scope='owner-v1', restored_activation=cache)
    assert [item.name for item in restored.snapshot.tools] == ['tool_search', 'second', 'first']
    assert resolve(source_scope='owner-v2', restored_activation=cache).get('first') is None
    changed = resolve([Tool('first', version='2'), Tool('second')], source_scope='owner-v1', restored_activation=cache)
    assert changed.get('first') is None
    assert {'name': 'first', 'reason': 'definition_changed'} in changed.restoration_diagnostics


def test_call_proof_is_one_definition_and_cannot_grow_with_unused_catalog():
    one = resolve(activated_tools={'archive.read'})
    many = resolve([Tool(), *(Tool(f'unused_{i}') for i in range(200))], activated_tools={'archive.read'})
    assert one.call_record('archive.read') == many.call_record('archive.read')
    proof = one.call_record('archive.read')
    assert proof['format'] == 'tool-call-v1'
    assert [item['name'] for item in proof['tools']] == ['archive.read']
    assert 'candidates' not in proof and 'policyDecisions' not in proof


def test_request_binding_does_not_need_an_in_memory_history():
    async def run():
        initial = resolve()
        context = ToolContext('r', 'role:r', 'run', CancellationToken(), 'search', initial.snapshot.snapshot_id, initial)
        await initial.get('tool_search').execute({'query': 'select:archive.read'}, context)
        initial.publish_revision(initial.pending_revision('search'))
        assert not hasattr(initial._discovery, 'revisions')
        assert initial._discovery.view(context) is initial
        assert initial.get('archive.read') is None
        assert initial.current().get('archive.read') is not None
    asyncio.run(run())


def test_only_completed_cache_restores_and_history_deletion_cannot_resurrect_it(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    for name, status, tools in [('good', 'completed', {'archive.read'}), ('bad', 'failed', set())]:
        run = AgentRun(runId=name, roleId='r', sessionKey='role:r', status='created', startedAt=datetime.now(timezone.utc))
        sessions.create_run_with_messages(run, "discover")
        sessions.update_run(name, status='running')
        sessions.save_tool_selection(name, resolve(run_id=name, activated_tools=tools).selection_cache())
        sessions.update_run(name, status=status)
    assert sessions.latest_tool_selection('r', 'role:r')['selected'][0]['name'] == 'archive.read'
    sessions.delete_role_session('r')
    assert sessions.latest_tool_selection('r', 'role:r') is None


def test_used_definition_audit_is_immutable_and_has_legacy_reader(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.create_run(AgentRun(runId='run', roleId='r', sessionKey='role:r', status='created', startedAt=datetime.now(timezone.utc)))
    proof = resolve(activated_tools={'archive.read'}).call_record('archive.read')
    kwargs = dict(run_id='run', role_id='r', tool_name='archive.read', call_id='call', argument_summary={}, capability_binding=proof)
    sessions.start_tool_audit(**kwargs)
    assert sessions.call_binding('run', 'call') == proof
    sessions.finish_tool_audit(run_id='run', call_id='call', result_category='uncertain')
    try:
        sessions.start_tool_audit(**kwargs)
    except ValueError:
        pass
    else:
        raise AssertionError('a repeated host ID must not erase/replay the prior audit')
    assert sessions.list_tool_audits('run')[0]['resultCategory'] == 'uncertain'
    assert 'capabilityRevisions' not in sessions.get_run('run').modelSnapshot


def test_request_uses_captured_executor_and_changed_declaration_is_stale():
    tool = Tool()
    resolution = resolve([tool], activated_tools={'archive.read'})
    context = ToolContext('r', 'role:r', 'run', CancellationToken())
    async def replaced(*args):
        return AgentToolResult('replacement must not receive old arguments')
    tool.execute = replaced
    assert asyncio.run(resolution.executor('archive.read')({}, context)).content == 'ok'
    tool.definition = replace(tool.definition, risk='mutating')
    assert resolution.authorization_denial(resolution.definition('archive.read'), context).startswith('stale_definition')


def test_clear_messages_and_changed_source_cannot_recover_selections(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.create_run_with_messages(AgentRun(runId='run', roleId='r', sessionKey='role:r', status='created',
        startedAt=datetime.now(timezone.utc)), 'discover')
    sessions.update_run('run', status='running')
    sessions.save_tool_selection('run', resolve(source_scope='sender-generation-1', activated_tools={'archive.read'}).selection_cache())
    sessions.update_run('run', status='completed')
    assert sessions.latest_tool_selection('r', 'role:r', source_scope='sender-generation-2') is None
    assert sessions.latest_tool_selection('r', 'role:r', source_scope='sender-generation-1') is not None
    with sqlite3.connect(sessions.database_path) as db:
        db.execute("DELETE FROM messages WHERE session_key='role:r'")
    assert sessions.latest_tool_selection('r', 'role:r', source_scope='sender-generation-1') is None


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


def test_restoration_remeasures_total_budget_and_never_waits_for_absent_tools():
    tools = [Tool(f'large_{i}') for i in range(3)]
    for tool in tools:
        tool.definition = replace(tool.definition, description='x' * 50000)
    previous = resolve(tools, activated_tools={tool.definition.name for tool in tools}).selection_cache()
    restored = resolve(tools, restored_activation=previous)
    assert len([item for item in restored.snapshot.tools if item.exposure == 'deferred']) == 2
    assert any(item['reason'] == 'budget' for item in restored.restoration_diagnostics)
    absent = resolve([Tool('other')], restored_activation=previous)
    assert all(item['reason'] == 'absent' for item in absent.restoration_diagnostics)
    assert absent.selection_cache()['selected'] == []


def test_spawn_parent_read_and_web_use_frozen_execution_methods(tmp_path):
    from backend.app.agent_runtime.spawn_tool import _ChildRead, _ChildTool
    parent = Tool('read_file')
    frozen = resolve([parent], activated_tools={'read_file'}).selected_subset({'read_file'})
    read = _ChildRead(parent, Tool('task_read'), tmp_path, frozen.executor('read_file'))
    web = _ChildTool(parent, frozen.definition('read_file'), lambda _: True, frozen.executor('read_file'))
    async def replacement(*args):
        return AgentToolResult('WRONG implementation')
    parent.execute = replacement
    context = ToolContext('r', 'role:r', 'run', CancellationToken())
    assert asyncio.run(read.execute({'path': '../parent'}, context)).content == 'ok'
    assert asyncio.run(web.execute({}, context)).content == 'ok'


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
    assert binding.current() is binding


def test_service_reconnect_rebinds_only_next_request_and_only_identical_definition():
    import pytest
    from backend.app.agent_runtime.discovery import ServiceGrant
    grant = ServiceGrant('grant', 'notes', 'r', frozenset({'read_only'}))
    initial = resolve(service_grants=(grant,))
    old = Tool('mcp_notes__read', source='mcp:notes')
    context = ToolContext('r', 'role:r', 'run', CancellationToken(), 'add', initial.snapshot.snapshot_id, initial)
    initial.stage_service_tools(context, grant_id='grant', parent_snapshot_id=initial.snapshot.snapshot_id, tools=(old,), activate=True)
    initial.publish_revision(initial.pending_revision('add'))
    request = initial.current()
    new = Tool('mcp_notes__read', source='mcp:notes')
    async def new_execute(*args):
        return AgentToolResult('new connection')
    new.execute = new_execute
    reconnect = replace(context, tool_call_id='reconnect', capability_snapshot_id=request.snapshot.snapshot_id, capability_binding=request)
    request.stage_service_tools(reconnect, grant_id='grant', parent_snapshot_id=request.snapshot.snapshot_id, tools=(new,), rebind=True)
    assert asyncio.run(request.executor(new.definition.name)({}, reconnect)).content == 'ok'
    request.publish_revision(request.pending_revision('reconnect'))
    assert asyncio.run(request.current().executor(new.definition.name)({}, reconnect)).content == 'new connection'
    assert request.current().snapshot.policy_decisions[new.definition.name] == 'allowed'
    assert request.current().denial_reason(new.definition.name) is None
    assert asyncio.run(request.executor(new.definition.name)({}, reconnect)).content == 'ok'
    new.definition = replace(new.definition, risk='mutating')
    with pytest.raises((PermissionError, ValueError)):
        request.current().stage_service_tools(replace(reconnect, tool_call_id='changed'), grant_id='grant',
            parent_snapshot_id=request.current().snapshot.snapshot_id, tools=(new,), rebind=True)


def test_namespace_metadata_change_invalidates_restoration():
    old = Tool(namespace_summary='private archive')
    cache = resolve([old], activated_tools={'archive.read'}).selection_cache()
    changed = Tool(namespace_summary='different authority description')
    restored = resolve([changed], restored_activation=cache)
    assert restored.get('archive.read') is None
    assert restored.restoration_diagnostics == ({'name': 'archive.read', 'reason': 'definition_changed'},)
