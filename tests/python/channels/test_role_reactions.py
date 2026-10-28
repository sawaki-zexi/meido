"""Automatic role reactions use real chat/runtime/media paths, no paid model."""
import asyncio
import base64
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_meme_turn import meme_turn, image_bytes, payload
from test_natural_turn import natural
from backend.app import main
from backend.app.model_adapter import ModelTextDelta, ModelResponseDone
from backend.app.session_manager import SessionManager


def illustration(state):
    raw = image_bytes('PNG')
    state.roles.update_illustrations(state.role.id, categories=['happy'])
    role = state.roles.add_illustration(state.role.id, raw, 'image/png', 'happy', category='happy')
    state.roles.update_illustrations(role.id, category_send={'happy': True})
    return role.illustrations[0], raw


class ReactionModel:
    def __init__(self, before_finish=lambda: None):
        self.before_finish = before_finish
        self.calls = 0

    async def stream_structured_messages(self, messages, configuration, tools):
        self.calls += 1
        # Select from the real host prompt, not a tool or caller-supplied path.
        token = re.search(r'role-[a-f0-9]{16}:', messages[0]['content']).group()[:-1]
        for piece in ('好呀！', '<me', 'me:', token, '>', ' ' * 140):
            yield ModelTextDelta(piece)
        self.before_finish()
        yield ModelResponseDone('stop')


@pytest.mark.parametrize('uncertain', [False, True])
def test_guest_ordinary_reply_sends_role_image_once_with_real_receipt(meme_turn, monkeypatch, uncertain):
    state = meme_turn
    item, raw = illustration(state)
    state.uncertain = uncertain
    model = ReactionModel()
    monkeypatch.setattr(main, 'model_adapter', model)

    async def scenario():
        try:
            inbound = payload('role-reaction', sender='22', image=False)
            await main._dispatch_channel_payload(state.adapter.account_id, inbound, source_adapter=state.adapter)
            await main._dispatch_channel_payload(state.adapter.account_id, inbound, source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    sends = [params for op, params in state.operations if op == 'send_group_msg']
    assert len(sends) == 1 and model.calls == 1
    image = next(segment for segment in sends[0]['message'] if segment['type'] == 'image')
    assert base64.b64decode(image['data']['file'].removeprefix('base64://')) == raw
    thread = state.channels.get_thread_by_chat(state.role.id, state.adapter.account_id, '100', chat_type='group')
    reply = state.sessions.list_messages(thread.session_key)[-1]
    assert reply.content == '好呀！'
    assert reply.metadata['deliveryStatus'] == ('uncertain' if uncertain else 'sent')
    assert len(reply.metadata['attachments']) == 1
    assert Path(reply.metadata['attachments'][0]['path']).read_bytes() == raw
    assert state.roles.illustration_path(state.role.id, item.id).read_bytes() == raw
    assert state.channels.runtime_reply_receipt(state.role.id, thread.session_key, reply.runId)['deliveryStatus'] == reply.metadata['deliveryStatus']


@pytest.mark.parametrize('change', ['permission', 'bytes'])
def test_generation_time_revoke_or_replacement_cannot_change_selected_image(meme_turn, monkeypatch, change):
    state = meme_turn
    item, _ = illustration(state)
    def revoke():
        if change == 'permission':
            state.roles.update_illustrations(state.role.id, category_send={'happy': False})
        else:
            state.roles.illustration_path(state.role.id, item.id).write_bytes(image_bytes('GIF'))
    monkeypatch.setattr(main, 'model_adapter', ReactionModel(revoke))
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id,
                payload('revoked', sender='22', image=False), source_adapter=state.adapter)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    sends = [params for op, params in state.operations if op == 'send_group_msg']
    assert sends and not any(segment['type'] == 'image' for send in sends for segment in send['message'])
    assert '<meme:' not in json.dumps(sends, ensure_ascii=False)


def test_desktop_stream_hides_protocol_and_persists_reaction_attachment(meme_turn, monkeypatch):
    state = meme_turn
    _, raw = illustration(state)
    monkeypatch.setattr(main, 'session_manager', SessionManager(state.roles, state.sessions))
    monkeypatch.setattr(main, 'model_adapter', ReactionModel())
    # Foreground model completion releases its runtime lock before reference
    # publication. Recovery must not retire that still-owned worker's stages.
    from backend.app.channels.meme_library import _OwnerPublication
    original_prepare = _OwnerPublication.prepare
    def prepare(publication, check):
        original_prepare(publication, check)
        publication.library.reconcile(publication.role_id)
        with publication.library.app._transaction() as db:
            row = db.execute('SELECT status,planned FROM autonomy_meme_operations WHERE operation_id=?',
                (publication.operation,)).fetchone()
            assert row['status'] == 'files_ready'
            assert json.loads(row['planned'])['principal'] == 'reaction'
    monkeypatch.setattr(_OwnerPublication, 'prepare', prepare)
    try:
        response = TestClient(main.app).post(f'/api/roles/{state.role.id}/messages', json={'content': '开心吗'})
        assert response.status_code == 200 and 'event: assistant_completed' in response.text
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert ''.join(event.get('delta', '') for event in events) == '好呀！'
        reply = next(event['message'] for event in reversed(events) if 'message' in event)
        assert reply['content'] == '好呀！' and len(reply['metadata']['attachments']) == 1
        identifier = reply['metadata']['attachments'][0]['id']
        fetched = TestClient(main.app).get(f'/api/roles/{state.role.id}/attachments/{identifier}')
        assert fetched.content == raw
        assert not state.operations  # desktop never dispatches to an account
    finally:
        if main._proactive is not None:
            asyncio.run(main._proactive.close())


def test_natural_qq_uses_enabled_role_illustrations_without_tools(natural, monkeypatch):
    state = natural
    _, raw = illustration(state)
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert not tools
            candidates = json.JSONDecoder().raw_decode(messages[0]['content'].split('候选：', 1)[1])[0]
            assert len(candidates) == 1
            yield ModelTextDelta(json.dumps({'action': 'reply', 'text': '好呀', 'candidate_id': candidates[0]['id']}))
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def scenario():
        try:
            data = payload('natural-role', sender='22', triggered=False, image=False)
            data['message'][0]['data']['text'] = '好喜欢你呀'
            event = await main.channel_host.accept_event(state.adapter.account_id, data)
            await main._handle_channel_event(event)
            await asyncio.sleep(0)
            task = state.scheduler.tasks.get(state.thread.thread_id)
            await state.clock.advance(1)
            if task is not None:
                await asyncio.wait_for(task, 5)
        finally:
            await state.scheduler.close()
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    sends = [params for op, params in state.operations if op == 'send_group_msg']
    assert len(sends) == 1
    image = next(segment for segment in sends[0]['message'] if segment['type'] == 'image')
    assert base64.b64decode(image['data']['file'].removeprefix('base64://')) == raw


@pytest.mark.parametrize('channel', ['telegram', 'feishu', 'qqbot'])
def test_supported_private_channels_send_reaction_through_real_adapter(meme_turn, monkeypatch, channel):
    from test_frozen_media import sender
    from backend.app.channels.models import TargetKind, target_from_chat
    state = meme_turn
    _, raw = illustration(state)
    received = []
    adapter = sender(channel, received)
    if channel == 'telegram':
        from test_host import MemoryCredentials
        state.channels.credentials = MemoryCredentials()
        account = state.channels.save_account(state.role.id, token='synthetic', enabled=True)
        adapter.account_id = account.account_id
    else:
        account = state.channels.register_channel_account(state.role.id, channel,
            account_id=adapter.account_id, enabled=True)
    main.channel_host.register(adapter)
    main.channel_adapters[adapter.account_id] = adapter
    thread = state.channels.ensure_channel_thread(state.role.id, channel, account.account_id, '101', 'private')
    _, user = state.channels.append_event(thread, direction='inbound', content='happy',
        external_message_id='reaction-trigger', metadata={'senderId': '101', 'senderIsOwner': False})
    target = target_from_chat(account_id=adapter.account_id, chat_id='101', kind=TargetKind.PRIVATE,
        external_message_id='reaction-trigger')
    state.channels.remember_targets(state.role.id, [target])
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            token = re.search(r'role-[a-f0-9]{16}:', messages[0]['content']).group()[:-1]
            yield ModelTextDelta('<meme:' + token + '>')
            yield ModelResponseDone('stop')
    monkeypatch.setattr(main, 'model_adapter', Model())
    async def scenario():
        try:
            await main._handle_channel_message(thread, 'happy', {'messageId': user.id,
                'senderId': '101', 'platformMessageId': 'reaction-trigger'}, False)
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(scenario())
    assert received == [raw]
    reply = state.sessions.list_messages(thread.session_key)[-1]
    assert reply.content == '' and reply.metadata['deliveryStatus'] == 'sent'
    assert len(reply.metadata['attachments']) == 1
