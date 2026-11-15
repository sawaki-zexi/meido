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
