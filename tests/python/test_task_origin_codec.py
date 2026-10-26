"""Historical origins retain identity without turning catalog IDs into grants."""
from dataclasses import asdict
import json

import pytest

from backend.app.channels.source_authority import FrozenChannelSource
from backend.app.novelai_generation.context import GenerationContext
from backend.app.proactive_runtime.identity import AutonomyOrigin
from backend.app.task_runtime import TaskOrigin


def channel_origin():
    source = FrozenChannelSource('r', 'thread:t', 't', 'a', '2026-01-01T00:00:00+00:00',
        'qq', 'chat', 'private', 'sender', True, 'identity', '2026-01-01T01:00:00+00:00',
        '2026-01-01T02:00:00+00:00', '2026-01-01T01:00:00+00:00')
    return TaskOrigin('r', 'thread:t', 'run', 'call', 'legacy-catalog', 't', 'a', 'binding',
                      json.dumps(source.to_dict()))


@pytest.mark.parametrize('catalog', [None, '', 'legacy-catalog', '7'])
def test_legacy_desktop_reads_and_positional_constructor_stay_compatible(catalog):
    raw = {'role_id': 'r', 'session_key': 'role:r', 'parent_run_id': 'run', 'tool_call_id': 'call'}
    if catalog is not None:
        raw['capability_version'] = catalog
    origin = TaskOrigin.from_dict(raw)
    assert origin == TaskOrigin('r', 'role:r', 'run', 'call', catalog or '')
    assert origin.grant_revision is None and origin.grant_domain == ''
    assert TaskOrigin.from_dict(origin.to_dict()) == origin
    assert TaskOrigin.from_dict(asdict(origin)) == origin


def test_legacy_autonomy_revision_has_its_own_domain_not_catalog_authority():
    autonomous = AutonomyOrigin('r', 'tick', 7, 3, ('drift',))
    current = autonomous.task_origin()
    assert current.capability_version == ''
    assert (current.grant_domain, current.grant_revision) == ('autonomy', 7)
    old = asdict(current)
    old.pop('grant_domain')
    old.pop('grant_revision')
    old['capability_version'] = '7'
    restored = TaskOrigin.from_dict(old)
    assert restored.scope() == current.scope()
    assert restored.capability_version == '7'
    assert (restored.grant_domain, restored.grant_revision) == ('autonomy', 7)


def test_legacy_source_verifier_revision_retains_original_proof():
    origin = TaskOrigin('r', 'source-verification:r', parent_run_id='attempt', capability_version='12',
        source_scope_id='owner-source-verification:attempt',
        source_authority_json=json.dumps({'source': 'desktop_owner_action', 'requestNonce': 'nonce',
            'generation': 4, 'sourceId': 'source', 'sourceRevision': 9}))
    raw = asdict(origin)
    raw.pop('grant_domain')
    raw.pop('grant_revision')
    restored = TaskOrigin.from_dict(raw)
    assert restored.scope() == origin.scope()
    assert restored.source_authority_json == origin.source_authority_json
    assert (restored.grant_domain, restored.grant_revision) == ('source-verification', 12)


def test_channel_and_novelai_use_the_same_strict_codec():
    original = channel_origin()
    assert TaskOrigin.from_dict(asdict(original)) == original
    context = GenerationContext(original, 'human', 'request', frozenset(),
        FrozenChannelSource.from_dict(json.loads(original.source_authority_json)), (1, 2))
    assert GenerationContext.from_dict(context.to_dict()).origin.scope() == original.scope()
    malformed = context.to_dict()
    malformed['origin']['sourceAuthority'] = None
    with pytest.raises(ValueError, match='source'):
        GenerationContext.from_dict(malformed)


@pytest.mark.parametrize('changes', [
    {'unexpected': 'ignored'}, {'roleId': 'different'}, {'role_id': 7},
    {'grant_domain': 'future', 'grant_revision': 1},
    {'grant_domain': 'autonomy', 'grant_revision': True},
    {'grant_domain': '', 'grant_revision': 1},
])
def test_unknown_mixed_or_malformed_origin_is_not_silently_repaired(changes):
    with pytest.raises(ValueError):
        TaskOrigin.from_dict({**asdict(TaskOrigin('r', 'role:r')), **changes})


@pytest.mark.parametrize('changes', [
    {'source_authority_json': ''}, {'source_account_id': 'other'}, {'source_thread_id': 'other'},
    {'source_scope_id': ''}, {'role_id': 'other'},
])
def test_channel_origin_missing_or_mismatched_frozen_evidence_is_rejected(changes):
    with pytest.raises(ValueError, match='source'):
        TaskOrigin.from_dict({**asdict(channel_origin()), **changes}, require_source_authority=True)


def test_legacy_domain_revision_cannot_disagree_with_embedded_authority():
    raw = asdict(AutonomyOrigin('r', 'tick', 7, 3, ('drift',)).task_origin())
    raw.pop('grant_domain')
    raw.pop('grant_revision')
    raw['capability_version'] = '8'
    with pytest.raises(ValueError, match='grant'):
        TaskOrigin.from_dict(raw)


def test_incomplete_legacy_channel_is_readable_for_query_but_not_new_effects():
    raw = {'role_id': 'r', 'session_key': 'thread:t', 'source_thread_id': 't', 'source_account_id': 'a'}
    origin = TaskOrigin.from_dict(raw)
    assert origin.scope() == ('r', 'thread:t', 't', 'a', '')
    assert origin.source_authority_json == ''
    with pytest.raises(ValueError, match='source'):
        TaskOrigin.from_dict(raw, require_source_authority=True)


def test_runtime_queries_and_stops_old_incomplete_source_without_replaying(tmp_path):
    import threading
    from backend.app.task_runtime import TaskBudget, TaskOutcome, TaskRuntime

    release, started = threading.Event(), threading.Event()

    class Owned:
        def run(self, execution):
            started.set()
            release.wait(3)
            return TaskOutcome('cancelled')

        def cancel(self):
            release.set()

    tasks = TaskRuntime(tmp_path / 'tasks')
    origin = TaskOrigin('r', 'role:r')
    try:
        identifier = tasks.submit(origin, Owned(), budget=TaskBudget(), kind='legacy')
        assert started.wait(3)
        old = {'role_id': 'r', 'session_key': 'thread:old', 'source_thread_id': 'old',
               'source_account_id': 'old-account', 'capability_version': 'old-catalog'}
        with tasks._lock, tasks._db:
            tasks._db.execute('UPDATE tasks SET origin=? WHERE task_id=?', (json.dumps(old), identifier))
        original_source = TaskOrigin.from_dict(old)
        assert tasks.query(identifier, original_source).origin.capability_version == 'old-catalog'
        tasks.stop(identifier, original_source)
        assert tasks.query(identifier, original_source).status == 'cancelled'
        with pytest.raises(ValueError, match='source'):
            TaskOrigin.from_dict(old, require_source_authority=True)
    finally:
        release.set()
        tasks.close()


def test_legacy_novelai_wire_identity_round_trips_without_new_blank_fields():
    value = GenerationContext(TaskOrigin('r', 'role:r'), 'human', 'request', frozenset()).to_dict()
    assert 'grantDomain' not in value['origin']
    assert GenerationContext.from_dict(value).to_dict() == value


@pytest.mark.parametrize('channel, chat_type', [('qq', 'private'), ('qq', 'group'), ('telegram', 'private')])
def test_original_complete_channel_task_remains_manageable_from_same_source_after_epoch_field_added(
    tmp_path, channel, chat_type,
):
    import hashlib
    import threading
    from dataclasses import replace
    from backend.app.task_runtime import TaskBudget, TaskOutcome, TaskRuntime

    def scope(payload):
        identity = {key: value for key, value in payload.items() if key != 'message_created_at'}
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    original = channel_origin()
    old_payload = json.loads(original.source_authority_json)
    old_payload.update(channel=channel, chat_type=chat_type)
    old_payload.pop('group_reply_revision', None)  # Actual pre-230 persisted shape.
    old_origin = replace(original, source_scope_id=scope(old_payload),
        source_authority_json=json.dumps(old_payload, sort_keys=True, separators=(',', ':')))
    old_origin = TaskOrigin.from_dict(asdict(old_origin), require_source_authority=True)
    # A later input has the same sender/binding, not the same message timestamp.
    source = replace(FrozenChannelSource.from_dict(old_payload), message_created_at='2026-01-01T03:00:00+00:00')
    current_payload = source.to_dict()
    current = replace(old_origin, tool_call_id='later-call', source_scope_id=scope(current_payload),
        source_authority_json=json.dumps(current_payload, sort_keys=True, separators=(',', ':')))
    started, release = threading.Event(), threading.Event()

    class Owned:
        def run(self, execution):
            started.set()
            release.wait(3)
            return TaskOutcome('cancelled')

        def cancel(self):
            release.set()

    tasks = TaskRuntime(tmp_path / 'tasks')
    try:
        identifier = tasks.submit(old_origin, Owned(), budget=TaskBudget(), kind='legacy')
        assert started.wait(3)
        assert tasks.query(identifier, current).origin == old_origin
        if channel == 'qq' and chat_type == 'group':
            changed_payload = replace(source, group_reply_revision=1).to_dict()
            assert changed_payload['group_reply_revision'] == 1
            changed = replace(current, source_scope_id=scope(changed_payload),
                source_authority_json=json.dumps(changed_payload))
            with pytest.raises(KeyError):
                tasks.query(identifier, changed)
        tasks.stop(identifier, current)
        assert tasks.query(identifier, current).status == 'cancelled'
    finally:
        release.set()
        tasks.close()
