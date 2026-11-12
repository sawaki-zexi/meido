"""History tools read the original group source within their frozen scope."""
import asyncio
import json
from datetime import datetime, timezone
from dataclasses import replace
from time import perf_counter

import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.agent_runtime.message_history_tools import build_message_history_tools
from backend.app.models import AgentRun
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore
from test_meme_turn import meme_turn  # noqa: F401
from test_natural_turn import natural  # noqa: F401


class MemoryCredentials:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key, "")

    def set(self, key, value):
        if value:
            self.values[key] = value
        else:
            self.values.pop(key, None)


def group_history(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(tmp_path / 'sessions.db', sessions, MemoryCredentials())
    channels.register_channel_account('role', 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = channels.ensure_channel_thread('role', 'qq', 'qq', 'group', 'group')
    channels.set_group_listening('role', 'qq', 'group', True)
    context = ToolContext('role', thread.session_key, 'query', CancellationToken())
    return sessions, channels, thread, context


def history_tools(sessions, channels, thread):
    return build_message_history_tools(sessions, channels, role_id=thread.role_id,
        source_thread_id=thread.thread_id, is_owner=False)


def execute(tool, arguments, context):
    return json.loads(asyncio.run(tool.execute(arguments, context)).content)


def hear(channels, thread, text, mid):
    return channels.listening.hear(thread, text, mid, {'senderId': 'member',
        'senderName': '群友', 'senderIsOwner': False, 'triggered': False})


def test_group_history_search_fetch_and_neighbors_use_listening_original(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    _, before = channels.append_event(thread, direction='inbound', content='前文', external_message_id='before')
    original = hear(channels, thread, '约定周三 15:30，带伞', 'heard')
    _, after = channels.append_event(thread, direction='inbound', content='后文', external_message_id='after')
    search, fetch = history_tools(sessions, channels, thread)
    found = execute(search, {'query': '15:30'}, context)
    assert [item['id'] for item in found['messages']] == [original['id']]
    assert found['messages'][0]['source']['senderId'] == 'member'
    fetched = execute(fetch, {'source_ref': original['id'], 'context': 1}, context)
    assert [item['id'] for item in fetched['messages']] == [before.id, original['id'], after.id]
    assert fetched['messages'][1]['content'] == original['content']
    assert fetched['messages'][1]['source']['sourceKind'] == 'listening'


def test_expired_overflow_cannot_be_read_from_legacy_natural_copy(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    channels.listening.set_cap('role', thread.thread_id, 1)
    durable = hear(channels, thread, '持久原文', 'stored')
    overflow = hear(channels, thread, '过期秘密', 'overflow')
    run = AgentRun(runId='natural', roleId='role', sessionKey=thread.session_key,
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'batchId': 'batch'})
    sessions.admit_natural_run(run, overflow)
    for number in range(30):
        hear(channels, thread, '临时窗口消息', str(number))
    search, fetch = history_tools(sessions, channels, thread)
    assert execute(search, {'query': '过期秘密'}, context)['count'] == 0
    result = execute(fetch, {'ids': [durable['id'], overflow['id']]}, context)
    assert [item['id'] for item in result['messages']] == [durable['id']]
    assert result['missing_source_refs'] == [overflow['id']]
    restarted = MultichannelStore(tmp_path / 'sessions.db', SessionStore(tmp_path / 'sessions.db'))
    result = execute(history_tools(restarted.sessions, restarted, thread)[1],
        {'ids': [durable['id'], overflow['id']]}, context)
    assert [item['id'] for item in result['messages']] == [durable['id']]


@pytest.mark.parametrize('change', ['clear', 'off_on', 'account', 'owner', 'edit'])
def test_listening_result_is_revalidated_before_return(tmp_path, monkeypatch, change):
    sessions, channels, thread, context = group_history(tmp_path)
    code = channels.create_pairing_code('role', 'qq')
    channels.consume_pairing_code(code, role_id='role', channel='qq', scope='platform',
        platform_user_id='owner', account_id='qq', chat_id='owner')
    original = hear(channels, thread, '读取期间的原文', 'heard')
    _, fetch = history_tools(sessions, channels, thread)
    read = fetch.history.read

    def mutate(context):
        result = read(context)
        if change == 'clear':
            channels.listening.clear('role', thread.thread_id)
        elif change == 'off_on':
            channels.set_group_listening('role', 'qq', 'group', False)
            channels.set_group_listening('role', 'qq', 'group', True)
        elif change == 'account':
            channels.delete_account('qq')
        elif change == 'owner':
            channels.unbind_identity(channels.list_identities()[0]['identityId'])
        else:
            with channels._connect() as db:
                db.execute('UPDATE channel_listening_records SET content=? WHERE id=?', ('修改后', original['id']))
        return result

    monkeypatch.setattr(fetch.history, 'read', mutate)
    with pytest.raises(PermissionError):
        execute(fetch, {'source_ref': original['id']}, context)


def test_fetch_long_original_has_bounded_pages_and_distinct_missing_status(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    original = hear(channels, thread, '第一部分' * 2500 + '第二部分' * 2500, 'long')
    _, fetch = history_tools(sessions, channels, thread)
    first = execute(fetch, {'ids': [original['id'], 'missing']}, context)
    assert len(json.dumps(first, ensure_ascii=False)) <= 24000
    item = first['messages'][0]
    assert item['truncated'] and item['next_content_offset'] == len(item['content'])
    second = execute(fetch, {'source_ref': original['id'], 'content_offset': item['next_content_offset']}, context)
    assert item['content'] + second['messages'][0]['content'] == original['content']
    assert first['missing_source_refs'] == ['missing']


def test_other_groups_and_owner_history_do_not_inherit_listening_access(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    other = channels.ensure_channel_thread('role', 'qq', 'qq', 'other-group', 'group')
    channels.set_group_listening('role', 'qq', other.chat_id, True)
    secret = hear(channels, other, '其他群的秘密', 'elsewhere')
    _, fetch = history_tools(sessions, channels, thread)
    missing = execute(fetch, {'source_ref': secret['id'], 'context': 10}, context)
    assert missing['count'] == 0 and missing['missing_source_refs'] == [secret['id']]
    own = hear(channels, thread, '当前群的原文', 'here')
    desktop = build_message_history_tools(sessions, channels, role_id='role')[1]
    result = execute(desktop, {'source_ref': own['id']}, ToolContext('role', 'role:role', 'web', CancellationToken()))
    assert result['count'] == 0
    with pytest.raises(PermissionError):
        execute(fetch, {'source_ref': own['id']}, ToolContext('other-role', thread.session_key, 'query', CancellationToken()))
    with pytest.raises(PermissionError):
        execute(fetch, {'source_ref': own['id']}, ToolContext('role', other.session_key, 'query', CancellationToken()))


def test_frozen_listening_view_allows_append_but_rejects_expired_ephemeral(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    channels.listening.set_cap('role', thread.thread_id, 1)
    durable = hear(channels, thread, '原来的持久记录', 'saved')
    live = hear(channels, thread, '仍在当前窗口', 'live')
    search, fetch = history_tools(sessions, channels, thread)
    assert execute(fetch, {'source_ref': live['id']}, context)['count'] == 1
    later = hear(channels, thread, '后来消息', 'later')
    assert execute(fetch, {'source_ref': later['id']}, context)['missing_source_refs'] == [later['id']]
    assert execute(fetch, {'source_ref': durable['id']}, context)['count'] == 1
    for number in range(30):
        hear(channels, thread, '新近临时消息', str(number))
    with pytest.raises(PermissionError):
        execute(search, {'query': '原来的'}, context)
    fresh = history_tools(sessions, channels, thread)[1]
    assert execute(fresh, {'source_ref': live['id']}, context)['missing_source_refs'] == [live['id']]


def test_new_query_after_listening_closes_reads_saved_records_only(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    channels.listening.set_cap('role', thread.thread_id, 1)
    saved = hear(channels, thread, '已保存原文', 'saved')
    temporary = hear(channels, thread, '临时原文', 'temporary')
    channels.set_group_listening('role', 'qq', 'group', False)
    _, fetch = history_tools(sessions, channels, thread)
    result = execute(fetch, {'ids': [saved['id'], temporary['id']]}, context)
    assert [item['id'] for item in result['messages']] == [saved['id']]
    assert result['missing_source_refs'] == [temporary['id']]


def test_cached_quote_body_does_not_bypass_original_visibility(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    quoted = channels.listening.hear(thread, '这是我的问题', 'quote', {
        'senderId': 'member', 'senderName': '群友', 'senderIsOwner': False,
        'replyTo': 'missing-original', 'replyContextVerified': True,
        'replyToSenderId': 'other', 'replyToContent': 'CACHED_PRIVATE_TEXT'})
    _, fetch = history_tools(sessions, channels, thread)
    result = execute(fetch, {'source_ref': quoted['id']}, context)
    assert 'CACHED_PRIVATE_TEXT' not in str(result)
    source = result['messages'][0]['source']
    assert source['senderId'] == 'member' and source['replyToSenderId'] == 'other'
    assert source['replyMissing'] is True and source['replyContentStatus'] == 'unavailable'


def test_duplicate_physical_sources_keep_both_original_references_resolvable(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    heard = hear(channels, thread, '同一条平台原文', 'same-platform')
    addressed = sessions.append_message(thread.session_key, 'user', '旧副本正文',
        metadata={**heard['metadata'], 'listening': False, 'triggered': True})
    search, fetch = history_tools(sessions, channels, thread)
    assert execute(search, {'query': '平台原文'}, context)['count'] == 1
    result = execute(fetch, {'ids': [heard['id'], addressed.id]}, context)
    assert result['matched_count'] == result['count'] == 1
    assert result['messages'][0]['content'] == heard['content']
    assert result['messages'][0]['id'] == heard['id']
    assert 'missing_source_refs' not in result


def test_budget_omission_can_be_retried_with_smaller_content_page(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    original = hear(channels, thread, '\x01' * 10000, 'escaped')
    _, fetch = history_tools(sessions, channels, thread)
    result = execute(fetch, {'source_ref': original['id']}, context)
    assert result['omitted_source_refs'] == [original['id']] and 'missing_source_refs' not in result
    assert len(json.dumps(result, ensure_ascii=False)) <= 24000
    result = execute(fetch, {'source_ref': original['id'], 'content_limit': 1000}, context)
    assert result['messages'][0]['content'] == '\x01' * 1000
    assert result['messages'][0]['next_content_offset'] == 1000


def test_many_matches_bound_result_and_prioritize_requested_originals(tmp_path):
    sessions, channels, thread, context = group_history(tmp_path)
    records = [hear(channels, thread, 'needle ' + '材料' * 500, str(number)) for number in range(52)]
    search, fetch = history_tools(sessions, channels, thread)
    found = execute(search, {'query': 'needle', 'limit': 50}, context)
    assert len(json.dumps(found, ensure_ascii=False)) <= 24000
    assert 0 < found['count'] <= 50 and found['omitted_count'] > 0
    assert found['next_offset'] == 50
    fetched = execute(fetch, {'source_refs': [record['id'] for record in records[:50]], 'context': 10}, context)
    assert len(json.dumps(fetched, ensure_ascii=False)) <= 24000
    assert fetched['count'] <= 50 and fetched['matched_count'] == 50
    assert fetched['omitted_count'] > 0 and 'missing_source_refs' not in fetched
    assert all(item['in_source_ref'] for item in fetched['messages'])
    missing = execute(fetch, {'ids': ['x' * 1990 + str(number) for number in range(50)]}, context)
    assert len(json.dumps(missing, ensure_ascii=False)) <= 24000
    assert missing['missing_count'] == 50 and missing['count'] == 0


def test_natural_turn_searches_and_fetches_saved_hearing(natural, monkeypatch, record_property):
    from backend.app import main
    from backend.app.model_adapter import ModelTextDelta, ModelToolCall, ModelResponseDone
    s = natural
    s.scheduler.limits = replace(s.scheduler.limits, max_calls=5)
    calls = []
    original = hear(s.channels, s.thread, 'HISTORIC_AGREEMENT 周三 15:30 出发', 'history')
    for number in range(31):
        hear(s.channels, s.thread, '后续话题', 'older-' + str(number))

    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            calls.append(messages)
            if len(calls) == 1:
                assert 'HISTORIC_AGREEMENT' not in str(messages)
                yield ModelTextDelta(json.dumps({'action': 'reply', 'response': {
                    'message_id': 'current', 'intent': '核对群里此前约定'}}))
            elif len(calls) == 2:
                yield ModelToolCall('search', 'search_messages', {'query': 'HISTORIC_AGREEMENT'})
                yield ModelResponseDone('tool_use')
            elif len(calls) == 3:
                result = json.loads(next(item['content'] for item in messages if item.get('tool_call_id') == 'search'))
                assert result['messages'][0]['id'] == original['id']
                assert result['messages'][0]['source']['senderId'] == 'member'
                yield ModelToolCall('fetch', 'fetch_messages', {'source_ref': original['id']})
                yield ModelResponseDone('tool_use')
            else:
                result = json.loads(next(item['content'] for item in messages if item.get('tool_call_id') == 'fetch'))
                assert result['messages'][0]['content'] == original['content']
                assert sum(item['role'] == 'tool' for item in messages) == 2
                yield ModelTextDelta('{"text":"原话约定周三15:30出发。"}')

    monkeypatch.setattr(main, 'model_adapter', Model())

    async def run():
        started = perf_counter()
        try:
            await s.receive('current')
            pending = tuple(s.scheduler.tasks.values())
            await s.clock.advance(1)
            await asyncio.wait_for(asyncio.gather(*pending), 10)
            assert len(calls) == 4 and len(s.operations) == 1
            records = s.sessions.list_messages(s.thread.session_key)
            assert [message.toolName for message in records if message.role == 'tool'] == ['search_messages', 'fetch_messages']
            record_property('listening_history_replay', json.dumps({'planning_calls': 1, 'expression_calls': 1,
                'query_continuations': 2, 'input_characters': [len(str(request)) for request in calls],
                'elapsed_seconds': round(perf_counter() - started, 3)}))
        finally:
            await s.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(run())
