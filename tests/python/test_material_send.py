"""Real search -> opaque choice -> original GIF -> synthetic platform -> durable receipt."""
import asyncio
import base64
import json
from types import SimpleNamespace
from dataclasses import replace

import pytest

from test_chat_meme_library import real_library, picture
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.channels.meme_tools import ChatMemeTools
from backend.app.channels.host import ChannelHost
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.channels.base import PlatformRejectedError
from backend.app.channels.models import TargetKind, target_from_chat
from backend.app.chat_attachments import resolve_attachment
from backend.app.multichannel import MultichannelStore
from backend.app.session_store import SessionStore


def setup(tmp_path, outcome='sent'):
    chat = real_library(tmp_path / 'library')
    root = chat.library.root('alice')
    (root / 'coding').mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': {
        'coding': {'desc': '猫捂脸表示编译失败', 'enabled': True}}}), encoding='utf-8')
    raw = picture('GIF')
    (root / 'coding/original.gif').write_bytes(raw)
    sessions = SessionStore(tmp_path / 'sessions.db')
    store = MultichannelStore(sessions.database_path, sessions)
    store.register_channel_account('alice', 'qq', account_id='qq', enabled=True, allow_group=True)
    thread = store.ensure_channel_thread('alice', 'qq', 'qq', '100', 'group')
    target = target_from_chat(account_id='qq', chat_id='100', kind=TargetKind.GROUP)
    store.remember_targets('alice', [target])
    state = SimpleNamespace(chat=chat, store=store, raw=raw, sent=[], allowed=True)
    async def transport(action, params):
        assert action == 'send_group_msg'
        state.sent.append(params)
        if outcome == 'failed':
            raise PlatformRejectedError('controlled rejection')
        if outcome == 'cancel':
            raise asyncio.CancelledError
        return {} if outcome == 'uncertain' else {'message_id': '101'}
    def host():
        value = ChannelHost(store, attachment_resolver=lambda role, identifier: resolve_attachment(chat.attachment_root(role), identifier))
        value.register(NapCatAdapter(NapCatConfig('qq'), transport=transport))
        return value
    state.host = host()
    state.context = ToolContext('alice', thread.session_key, 'stable-run', CancellationToken(), 'call')
    def tools(*, target_id=target.id, new_host=False):
        service = ChatMemeTools(chat, host() if new_host else state.host, role_id='alice', session_key=thread.session_key,
            run_id=state.context.run_id, account_id='qq', thread_id=thread.thread_id, target_id=target_id,
            attachments=(), authorized=lambda: state.allowed, permitted=lambda name: True, owner_text='找编译失败的图并发送')
        return {tool.definition.name.removeprefix('chat_meme_'): tool for tool in service.tools()}
    state.tools = tools
    return state


@pytest.mark.parametrize('outcome', ['sent', 'failed', 'uncertain', 'cancel'])
def test_real_tool_search_and_original_delivery_replay(outcome, tmp_path):
    state = setup(tmp_path, outcome)
    async def scenario():
        tools = state.tools()
        listed = await tools['list'].execute({'query': '编译又失败了'}, state.context)
        token = listed.details['candidates'][0]['id']
        args = {'candidate_id': token, 'operation_id': 'my-selection'}
        if outcome == 'cancel':
            with pytest.raises(asyncio.CancelledError):
                await tools['send'].execute(args, state.context)
        else:
            first = await tools['send'].execute(args, state.context)
            assert first.details['status'] == outcome
        # Reconstructed host and tools reconcile the original ledger, including pending/unknown.
        tools = state.tools(new_host=True)
        again = await tools['list'].execute({'query': '编译'}, state.context)
        assert again.details['candidates'][0]['id'] != token
        args['candidate_id'] = again.details['candidates'][0]['id']
        replay = await tools['send'].execute(args, state.context)
        assert replay.details['status'] == ('uncertain' if outcome == 'cancel' else outcome)
        assert len(state.sent) == 1
        if outcome in {'uncertain', 'cancel'}:
            tools = state.tools(new_host=True)
            again = await tools['list'].execute({'query': '编译'}, state.context)
            args['candidate_id'] = again.details['candidates'][0]['id']
            substitute = await tools['send'].execute({**args, 'operation_id': 'replacement', 'content': '换个说法'}, state.context)
            assert substitute.details['status'] == 'uncertain' and len(state.sent) == 1
        image = next(item for item in state.sent[0]['message'] if item['type'] == 'image')
        assert base64.b64decode(image['data']['file'].removeprefix('base64://')) == state.raw
        usage, = state.store.material_usage('alice').values()
        assert usage['attempts'] == 1 and usage['successes'] == int(outcome == 'sent')
        # A fresh facade must surface a definite conflict, not swallow it as unknown.
        tools = state.tools(new_host=True)
        again = await tools['list'].execute({'query': '编译'}, state.context)
        args['candidate_id'] = again.details['candidates'][0]['id']
        conflict = await tools['send'].execute({**args, 'content': 'different'}, state.context)
        assert conflict.is_error and 'operation parameters changed' in conflict.content
        assert len(state.sent) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('reason', ['target', 'run', 'expiry', 'revoked', 'disabled', 'replacement'])
def test_old_credential_never_authorizes_new_dispatch(tmp_path, reason):
    state = setup(tmp_path)
    async def scenario():
        tools = state.tools()
        result = await tools['list'].execute({'query': '编译'}, state.context)
        token = result.details['candidates'][0]['id']
        context = state.context
        if reason == 'target':
            tools = state.tools(target_id='different-target')
            result = await tools['list'].execute({'query': '编译'}, context)
            assert result.details['candidates'][0]['id'] != token
        elif reason == 'run':
            context = replace(context, run_id='different-run')
        elif reason == 'expiry':
            item, _ = tools['send'].owner.candidates[token]
            tools['send'].owner.candidates[token] = (item, 0)
        elif reason == 'revoked':
            state.allowed = False
        elif reason == 'disabled':
            path = state.chat.library.root('alice') / 'manifest.json'
            document = json.loads(path.read_text(encoding='utf-8'))
            document['categories']['coding']['enabled'] = False
            path.write_text(json.dumps(document), encoding='utf-8')
        else:
            (state.chat.library.root('alice') / 'coding/original.gif').write_bytes(picture('GIF', 'purple'))
        sent = await tools['send'].execute({'candidate_id': token}, context)
        assert sent.is_error and not state.sent
    asyncio.run(scenario())


def test_uncertain_original_blocks_plain_text_only_in_its_original_run(tmp_path):
    state = setup(tmp_path, 'uncertain')
    async def scenario():
        tools = state.tools()
        listed = await tools['list'].execute({'query': '编译'}, state.context)
        await tools['send'].execute({'candidate_id': listed.details['candidates'][0]['id']}, state.context)
        owner = tools['send'].owner
        async def text(run_id):
            return await state.host.account_send('alice', 'qq', owner.target_id, '说明文字', actor='reply',
                source_thread_id=owner.thread_id, source_session_key=owner.session_key,
                source_run_id=run_id, source_kind='reply', authorize_source=lambda: True)
        await text(state.context.run_id)
        assert len(state.sent) == 1
        await text('new-user-turn')
        assert len(state.sent) == 2
    asyncio.run(scenario())


def test_explicit_tool_can_continue_after_default_four(tmp_path):
    state = setup(tmp_path)
    for number in range(7):
        (state.chat.library.root('alice') / 'coding' / f'{number}.png').write_bytes(picture(color=(number, 2, 3)))
    async def scenario():
        tools = state.tools()
        first = await tools['list'].execute({'query': '编译'}, state.context)
        assert len(first.details['candidates']) == 4 and first.details['nextOffset'] == 4
        second = await tools['list'].execute({'query': '编译', 'offset': first.details['nextOffset']}, state.context)
        assert len(second.details['candidates']) == 4 and second.details['nextOffset'] is None
        assert not {item['id'] for item in first.details['candidates']} & {item['id'] for item in second.details['candidates']}
    asyncio.run(scenario())


def test_relisting_does_not_renew_an_expired_selection(tmp_path, monkeypatch):
    import backend.app.channels.meme_tools as module
    state = setup(tmp_path)
    now = [10.0]
    # Replace the module reference, not asyncio's shared time module.
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    async def scenario():
        tools = state.tools()
        first = await tools['list'].execute({'query': '编译'}, state.context)
        old = first.details['candidates'][0]['id']
        now[0] += 91
        second = await tools['list'].execute({'query': '编译'}, state.context)
        new = second.details['candidates'][0]['id']
        assert old != new
        assert (await tools['send'].execute({'candidate_id': old}, state.context)).is_error
        assert not state.sent
        assert not (await tools['send'].execute({'candidate_id': new}, state.context)).is_error
        assert len(state.sent) == 1
    asyncio.run(scenario())


def test_cancel_joins_original_reference_preparation_before_returning(tmp_path, monkeypatch):
    import threading
    state = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = state.chat.reference
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(state.chat, 'reference', blocked)
    async def scenario():
        tools = state.tools()
        result = await tools['list'].execute({'query': '编译'}, state.context)
        pending = asyncio.create_task(tools['send'].execute({'candidate_id': result.details['candidates'][0]['id']}, state.context))
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(.01)
        assert not pending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 3)
        assert not state.sent
        private = state.chat.library.root('alice').parent / '.meido-memes/staging'
        assert not list(private.glob('*.tmp'))
    asyncio.run(scenario())
