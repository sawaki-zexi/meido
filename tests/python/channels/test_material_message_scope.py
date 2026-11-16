"""Trusted image clicks retain actual QQ scope through restart and ordinary sends."""
import asyncio
import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_meme_turn import meme_turn, payload, image_bytes
from backend.app import main
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.chat_attachments import save_attachment
from backend.app.channels.natural_media import NaturalMedia
from backend.app.channels.role_reactions import RoleReactionLibrary
from backend.app.model_adapter import ModelResponseDone, ModelTextDelta, ModelToolCall


def collect(state, description='本群开心表情'):
    raw = image_bytes('GIF')
    attachment = save_attachment(state.roles.workspace_path(state.role.id), 'original.gif', 'image/gif', raw)
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
    _, message = state.channels.append_event(thread, direction='inbound', content='这张图片',
        external_message_id='saved-image', metadata={'senderId': '11', 'senderIsOwner': True,
            'groupReplyRevision': state.channels.group_reply_policy(state.role.id, state.adapter.account_id, '100')[1],
            'attachments': [attachment]})
    response = TestClient(main.app).post(f'/api/roles/{state.role.id}/materials/from-message', json={
        'messageId': message.id, 'attachmentId': attachment['id'], 'description': description})
    assert response.status_code == 200, response.text
    assert response.json()['material']['sources'] == [{'kind': 'chat', 'scope': '当前会话'}]
    return thread, attachment, raw, response.json()


@pytest.mark.parametrize('revoke', ['group', 'rebind', 'account'])
def test_real_message_collection_survives_offline_but_not_revoked_scope(meme_turn, tmp_path, revoke):
    state = meme_turn
    thread, attachment, raw, saved = collect(state)
    context = ToolContext(state.role.id, thread.session_key, 'later', CancellationToken(), 'choose')
    library = main._chat_meme_library()
    assert library.candidates(state.role.id) == ()
    selected, = library.candidates(state.role.id, context=context)
    # The transport is actually absent and the input copy removed; durable
    # collection still has its local original and original authorization.
    main.channel_host.unregister(state.adapter.account_id)
    Path(attachment['path']).unlink()
    assert library.reference(state.role.id, selected, context)[1] == raw
    main.channel_host.register(state.adapter)
    assert library.valid(state.role.id, selected)
    # The public owner editor can correct a scoped record without sharing it.
    response = TestClient(main.app).patch(f'/api/roles/{state.role.id}/materials/{saved["material"]["id"]}',
        json={'expectedRevision': saved['revision'], 'description': '本群纠正后的说明'})
    assert response.status_code == 200, response.text
    assert response.json()['material']['sources'] == saved['material']['sources']
    assert not library.valid(state.role.id, selected)
    selected, = library.candidates(state.role.id, context=context)
    assert library.candidates(state.role.id) == ()

    async def natural_candidates():
        media = NaturalMedia(RoleReactionLibrary(library.library, state.roles,
            source_validator=main._material_source_authorized), tmp_path / 'cache')
        batch = await media.prepare(role_id=state.role.id, account_id=state.adapter.account_id,
            thread_id=thread.thread_id, chat_id='100', batch_id='scope', inputs=(), deadline=media.clock() + 10,
            authorized=lambda: True, context=context, vision_enabled=False, spend=lambda n: True)
        assert len(batch.candidates) == 1 and batch.candidates[0]['description'] == '本群纠正后的说明'
        batch.close()
    asyncio.run(natural_candidates())
    if revoke == 'group':
        state.channels.set_group_replies(state.role.id, state.adapter.account_id, '100', False)
        state.channels.set_group_replies(state.role.id, state.adapter.account_id, '100', True)
    elif revoke == 'rebind':
        state.channels.unbind_identity(state.channels.list_identities()[0]['identityId'])
        code = state.channels.create_pairing_code(state.role.id, 'qq')
        assert state.channels.consume_pairing_code(code, role_id=state.role.id, channel='qq', scope='platform',
            platform_user_id='11', account_id=state.adapter.account_id, chat_id='11')
    else:
        state.channels.set_account_state(state.adapter.account_id, enabled=False)
    assert library.candidates(state.role.id, context=context) == ()
    assert not library.valid(state.role.id, selected)
    asyncio.run(main._proactive.close())


def test_pending_gif_collection_restarts_and_sends_only_by_explicit_owner_tool(meme_turn, monkeypatch):
    state = meme_turn
    thread, _, raw, saved = collect(state, '')
    assert saved['material']['semanticStatus'] == 'pending'
    asyncio.run(main._proactive.close())
    monkeypatch.setattr(main, '_proactive', None)
    library = main._chat_meme_library()
    context = ToolContext(state.role.id, thread.session_key, 'later', CancellationToken(), 'choose')
    assert library.candidates(state.role.id, context=context) == ()

    class Model:
        calls = 0
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            results = {item['tool_call_id']: json.loads(item['content']) for item in messages if item['role'] == 'tool'}
            if self.calls == 1:
                yield ModelToolCall('list', 'chat_meme_list', {})
                yield ModelResponseDone('tool_use')
            elif self.calls == 2:
                candidate, = results['list']['candidates']
                assert candidate['semanticStatus'] == 'pending' and candidate['description'] == ''
                yield ModelToolCall('send', 'chat_meme_send', {'candidate_id': candidate['id']})
                yield ModelResponseDone('tool_use')
            else:
                assert results['send']['status'] == 'sent'
                yield ModelTextDelta('已发送。')
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def scenario():
        try:
            event = payload('explicit-send', image=False)
            event['message'][-1]['data']['text'] = '把刚才收藏的表情发到本群'
            await main._dispatch_channel_payload(state.adapter.account_id, event, source_adapter=state.adapter)
        finally:
            await main._proactive.close()
    asyncio.run(scenario())
    images = [segment for operation, params in state.operations if operation == 'send_group_msg'
        for segment in params['message'] if segment['type'] == 'image']
    assert len(images) == 1
    assert base64.b64decode(images[0]['data']['file'].removeprefix('base64://')) == raw


def test_duplicate_group_source_never_reauthorizes_another_groups_meaning(meme_turn):
    state = meme_turn
    a, attachment, raw, saved = collect(state, 'A群秘密含义')
    b = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '200', 'group')
    def save_b(identifier, description=''):
        other = save_attachment(state.roles.workspace_path(state.role.id), identifier + '.gif', 'image/gif', raw)
        _, message = state.channels.append_event(b, direction='inbound', content='收藏这张',
            external_message_id=identifier, metadata={'senderId': '11', 'senderIsOwner': True,
                'groupReplyRevision': state.channels.group_reply_policy(state.role.id, state.adapter.account_id, '200')[1],
                'attachments': [other]})
        response = TestClient(main.app).post(f'/api/roles/{state.role.id}/materials/from-message', json={
            'messageId': message.id, 'attachmentId': other['id'], 'description': description})
        assert response.status_code == 200, response.text
        return response.json()
    try:
        save_b('b-blank')
        library = main._chat_meme_library()
        ctx_a = ToolContext(state.role.id, a.session_key, 'a', CancellationToken(), 'choose')
        ctx_b = ToolContext(state.role.id, b.session_key, 'b', CancellationToken(), 'choose')
        old_a, = library.candidates(state.role.id, context=ctx_a)
        assert library.candidates(state.role.id, context=ctx_b) == ()
        pending, = library.candidates(state.role.id, context=ctx_b, explicit=True)
        assert library.reference(state.role.id, pending, ctx_b)[1] == raw
        state.channels.set_group_replies(state.role.id, state.adapter.account_id, '100', False)
        assert not library.valid(state.role.id, old_a)
        assert library.candidates(state.role.id, context=ctx_b) == ()
        assert save_b('b-new-attachment', 'B群补充说明')['descriptionOutcome'] == 'filled'
        own, = library.candidates(state.role.id, context=ctx_b)
        assert own.description == 'B群补充说明'
        record = json.loads(next((state.root.parent / '.meido-memes/chat').glob('*.json')).read_text(encoding='utf-8'))
        assert len(record['scopedMeanings']) == 1  # New message/attachment is still the same authority scope.
        state.channels.set_group_replies(state.role.id, state.adapter.account_id, '200', False)
        state.channels.set_group_replies(state.role.id, state.adapter.account_id, '200', True)
        save_b('b-new-permission')
        assert not library.valid(state.role.id, own)
        assert library.candidates(state.role.id, context=ctx_b) == ()
        fresh, = library.candidates(state.role.id, context=ctx_b, explicit=True)
        assert fresh.description == '' and library.reference(state.role.id, fresh, ctx_b)[1] == raw
    finally:
        asyncio.run(main._proactive.close())


def test_group_editor_promotion_never_restores_real_pairing_generation(meme_turn):
    state = meme_turn
    thread, attachment, raw, saved = collect(state, '群内编译失败梗')
    client = TestClient(main.app)
    endpoint = f'/api/roles/{state.role.id}/materials'
    library = main._chat_meme_library()
    context = ToolContext(state.role.id, thread.session_key, 'edit-scope', CancellationToken(), 'choose')
    try:
        item = saved['material']
        group = next(scope for scope in item['meaningScopes'] if scope['scope'] == 'conversation')
        promoted = client.patch(endpoint + '/' + item['id'], json={'expectedRevision': saved['revision'],
            'meaningScopeId': 'role', 'description': '通用尴尬', 'uses': ['尴尬']})
        assert promoted.status_code == 200, promoted.text
        selected, = library.candidates(state.role.id, '尴尬', context=context)
        assert selected.public()['groupDescription'] == '群内编译失败梗'
        assert library.candidates(state.role.id) == ()
        state.channels.unbind_identity(state.channels.list_identities()[0]['identityId'])
        code = state.channels.create_pairing_code(state.role.id, 'qq')
        assert state.channels.consume_pairing_code(code, role_id=state.role.id, channel='qq', scope='platform',
            platform_user_id='11', account_id=state.adapter.account_id, chat_id='11')
        assert not library.valid(state.role.id, selected)
        latest = client.get(endpoint).json()
        for selection in ({}, {'meaningScopeId': group['id']}):
            response = client.patch(endpoint + '/' + item['id'], json={'expectedRevision': latest['revision'],
                'description': '重新编辑', **selection})
            # The default target is now shared; it may be corrected without
            # authorizing the original. An explicit stale group cannot be edited.
            if selection:
                assert response.status_code == 403, response.text
            else:
                assert response.status_code == 200, response.text
                latest = client.get(endpoint).json()
            assert library.candidates(state.role.id, context=context) == ()
    finally:
        asyncio.run(main._proactive.close())
