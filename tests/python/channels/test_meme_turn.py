"""QQ sticker acceptance through real parsing, Runtime, library and delivery.

Only the model and remote platform/media transport are controlled doubles.
"""
import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from backend.app import main
from backend.app.agent_runtime.runtime import RuntimeManager
from backend.app.channels import ChannelHost, media
from backend.app.channels.base import PlatformRejectedError
from backend.app.channels.group_tools import MultichannelGroupRepository
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.chat_attachments import resolve_attachment
from backend.app.memory_config import MemorySettingsStore
from backend.app.model_adapter import ModelTextDelta, ModelToolCall
from backend.app.model_config import ModelConfigurationStore
from backend.app.models import RoleInput, RoleProfile
from backend.app.multichannel import MultichannelStore
from backend.app.role_store import RoleStore
from backend.app.session_store import SessionStore
from backend.app.task_runtime import TaskRuntime


@pytest.fixture
def meme_turn(tmp_path, monkeypatch, local_platform_only):
    roles = RoleStore(tmp_path / 'roles')
    role = roles.create(RoleInput(name='群表情验收', profile=RoleProfile(profile='测试')))
    sessions = SessionStore(tmp_path / 'sessions.db')
    channels = MultichannelStore(sessions.database_path, sessions)
    account = channels.register_channel_account(role.id, 'qq', account_id='qq-memes', enabled=True, allow_group=True)
    code = channels.create_pairing_code(role.id, 'qq')
    assert channels.consume_pairing_code(code, role_id=role.id, channel='qq', scope='platform',
        platform_user_id='11', account_id=account.account_id, chat_id='11')
    settings = MemorySettingsStore(tmp_path / 'memory-settings.json')
    settings.update(enabled=False)
    tasks = TaskRuntime(tmp_path / 'tasks')
    state = SimpleNamespace(roles=roles, role=role, sessions=sessions, channels=channels,
        operations=[], downloads=[], raw=b'', extension='png', media_type='image/png', uncertain=False)

    async def transport(operation, params):
        state.operations.append((operation, params))
        if operation == 'get_msg':
            quoted = {'sender': {'user_id': 99, 'nickname': '角色'}, 'message': [
                {'type': 'image', 'data': {'file': 'quoted.' + state.extension, 'url': 'https://qq.test/image'}},
            ]}
            if getattr(state, 'quote_group', 100) is not None:
                quoted['group_id'] = getattr(state, 'quote_group', 100)
            if getattr(state, 'quote_type', None) is not None:
                quoted['message_type'] = state.quote_type
            return {'data': quoted}
        if operation in {'get_friend_list', 'get_group_list', 'get_group_member_list'}:
            return {'data': []}
        assert operation == 'send_group_msg'
        assert params['group_id'] == 100
        if getattr(state, 'reject_image', False) and any(segment['type'] == 'image' for segment in params['message']):
            raise PlatformRejectedError('controlled platform rejection')
        if state.uncertain and any(segment['type'] == 'image' for segment in params['message']):
            return {}  # Accepted request with no valid receipt is uncertain, not failed.
        return {'message_id': str(1000 + len(state.operations))}

    async def download(url, *, max_bytes):
        state.downloads.append(url)
        if getattr(state, 'download_error', False) or url in getattr(state, 'download_failures', set()):
            raise OSError('controlled download failure')
        assert url in {'https://qq.test/image', 'https://qq.test/second-image'}
        assert len(state.raw) <= max_bytes
        return state.raw, state.media_type

    async def no_avatar(*args, **kwargs):
        return None

    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=transport)
    host = ChannelHost(channels, attachment_resolver=lambda rid, aid: resolve_attachment(roles.workspace_path(rid), aid))
    host.register(adapter)
    for name, value in {
        'store': roles, 'session_store': sessions, 'multichannel_store': channels,
        'channel_host': host, 'channel_adapters': {adapter.account_id: adapter},
        'group_repository': MultichannelGroupRepository(channels),
        'model_configuration_store': ModelConfigurationStore(tmp_path / 'models.json'),
        'runtime_manager': RuntimeManager(sessions), 'role_locks': {}, 'channel_event_locks': {},
        'memory_settings_store': settings, 'task_runtime': tasks, '_proactive': None,
        '_channel_attachment_usage': {}, '_fetch_channel_avatar': no_avatar,
    }.items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(media, 'download_qq_media', download)
    root = roles.workspace_path(role.id) / 'memes'
    (root / 'happy').mkdir(parents=True)
    state.manifest = {'version': 1, 'categories': {'happy': {'desc': '主人手写说明', 'aliases': ['开心'], 'enabled': True}}}
    (root / 'manifest.json').write_text(json.dumps(state.manifest, ensure_ascii=False), encoding='utf-8')
    state.root, state.adapter = root, adapter
    yield state
    tasks.close()


def image_bytes(fmt):
    stream = io.BytesIO()
    first = Image.new('RGB', (8, 7), 'red')
    options = {'save_all': True, 'append_images': [Image.new('RGB', (8, 7), 'blue')], 'duration': 80, 'loop': 0} if fmt == 'GIF' else {}
    first.save(stream, format=fmt, **options)
    return stream.getvalue()


def payload(identifier, *, quoted=False, sender='11', triggered=True, image=True):
    segments = [{'type': 'text', 'data': {'text': '请把这张图片收录到 happy 表情库，然后发给本群'}}]
    if triggered:
        # Omit sender_id deliberately: accept_event must resolve the actual bot quote.
        segments.insert(0, {'type': 'reply', 'data': {'id': 'quoted-message'}} if quoted
                        else {'type': 'at', 'data': {'qq': '99'}})
    if image and not quoted:
        segments.append({'type': 'image', 'data': {'file': 'current', 'url': 'https://qq.test/image'}})
    return {'post_type': 'message', 'message_type': 'group', 'self_id': 99, 'group_id': 100,
        'message_id': identifier, 'sender': {'user_id': sender, 'nickname': '测试发送者'}, 'message': segments}


@pytest.mark.parametrize('quoted', [False, True], ids=['at-current', 'quote-only'])
@pytest.mark.parametrize('fmt,extension,mime', [('PNG', 'png', 'image/png'), ('JPEG', 'jpg', 'image/jpeg'),
    ('WEBP', 'webp', 'image/webp'), ('GIF', 'gif', 'image/gif')])
@pytest.mark.parametrize('uncertain', [False, True], ids=['sent', 'uncertain'])
def test_owner_saves_and_reuses_original_sticker_with_drift_off(meme_turn, monkeypatch, quoted, fmt, extension, mime, uncertain):
    state = meme_turn
    state.raw, state.extension, state.media_type, state.uncertain = image_bytes(fmt), extension, mime, uncertain
    class Model:
        calls = 0
        finished = False
        candidate = None
        results = {}

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            names = {tool['function']['name'] for tool in tools}
            assert {'chat_meme_save', 'chat_meme_list', 'chat_meme_send'} <= names
            results = {item['tool_call_id']: item['content'] for item in messages if item['role'] == 'tool'}
            self.results = results
            if self.calls in {1, 2}:
                if self.calls == 2:
                    assert json.loads(results['save-1'])['status'] == 'recorded'
                yield ModelToolCall(f'save-{self.calls}', 'chat_meme_save', {'category': 'happy', 'description': '红蓝测试表情'})
            elif self.calls == 3:
                assert json.loads(results['save-2'])['deduplicated'] is True
                yield ModelToolCall('list', 'chat_meme_list', {'query': '开心'})
            elif self.calls == 4:
                candidates = json.loads(results['list'])['candidates']
                assert len(candidates) == 1
                self.candidate = candidates[0]['id']
                yield ModelToolCall('send', 'chat_meme_send', {'candidate_id': self.candidate, 'content': '给你表情'})
            elif uncertain and self.calls == 5:
                assert json.loads(results['send'])['status'] == 'uncertain'
                yield ModelToolCall('repeat-send', 'chat_meme_send', {'candidate_id': self.candidate, 'content': '给你表情'})
            else:
                self.finished = True
                yield ModelTextDelta('已收录；投递结果不确定，不重复发送。' if uncertain else '表情已送达。')

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, payload('first', quoted=quoted), source_adapter=state.adapter)
            assert model.finished, f'Controlled model did not reach final receipt: {model.results}'
            assert main._proactive_application().drift_skills.configuration(state.role.id).driftEnabled is False
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    images = [file for file in (state.root / 'happy').iterdir() if file.suffix.lower() in {'.png', '.jpg', '.webp', '.gif'}]
    assert len(images) == 1 and images[0].read_bytes() == state.raw
    assert json.loads((state.root / 'manifest.json').read_text(encoding='utf-8')) == state.manifest
    sends = [params for operation, params in state.operations if operation == 'send_group_msg'
             and any(part['type'] == 'image' for part in params['message'])]
    assert len(sends) == 1, 'Unknown receipt must never trigger a second image send'
    image = next(part for part in sends[0]['message'] if part['type'] == 'image')
    assert base64.b64decode(image['data']['file'].removeprefix('base64://')) == state.raw
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
    messages = state.sessions.list_messages(thread.session_key)
    inbound = next(message for message in messages if message.role == 'user')
    original = inbound.metadata['quotedAttachments' if quoted else 'attachments'][0]
    assert Path(original['path']).read_bytes() == state.raw
    outbound = [message for message in messages if message.metadata.get('attachments') and message.role == 'assistant']
    assert len(outbound) == 1
    assert outbound[0].metadata['deliveryStatus'] == ('uncertain' if uncertain else 'sent')
    for message in messages:
        if message.messageType == 'tool_result':
            assert message.isError is (uncertain and message.toolCallId in {'send', 'repeat-send'})


@pytest.mark.parametrize('quoted', [False, True], ids=['at', 'quote-only'])
def test_guest_cannot_get_meme_tools_and_untriggered_owner_does_not_run(meme_turn, monkeypatch, quoted):
    state = meme_turn
    state.raw = image_bytes('PNG')
    class Model:
        calls = 0
        finished = False
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert not {'chat_meme_save', 'chat_meme_list', 'chat_meme_send'} & {tool['function']['name'] for tool in tools}
            self.finished = True
            yield ModelTextDelta('不能收录。')
    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, payload('guest', sender='22', quoted=quoted), source_adapter=state.adapter)
            assert model.calls == 1 and model.finished
            await main._dispatch_channel_payload(state.adapter.account_id, payload('untriggered', triggered=False), source_adapter=state.adapter)
            assert model.calls == 1
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert list((state.root / 'happy').iterdir()) == []


def test_enabled_legacy_library_image_needs_no_registration_and_reuses_reference(meme_turn, monkeypatch):
    state = meme_turn
    state.raw = image_bytes('PNG')
    (state.root / 'happy' / 'old.png').write_bytes(state.raw)

    class Model:
        def __init__(self, label):
            self.label, self.calls, self.finished = label, 0, False

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert {'chat_meme_list', 'chat_meme_send'} <= {tool['function']['name'] for tool in tools}
            results = {item['tool_call_id']: item['content'] for item in messages if item['role'] == 'tool'}
            if self.calls == 1:
                yield ModelToolCall(self.label + '-list', 'chat_meme_list', {'query': '开心'})
            elif self.calls == 2:
                candidates = json.loads(results[self.label + '-list'])['candidates']
                assert len(candidates) == 1
                yield ModelToolCall(self.label + '-send', 'chat_meme_send', {'candidate_id': candidates[0]['id']})
            else:
                assert json.loads(results[self.label + '-send'])['status'] == 'sent'
                self.finished = True
                yield ModelTextDelta('表情已发送。')

    async def scenario():
        try:
            counts = []
            for number in range(2):
                model = Model(str(number))
                monkeypatch.setattr(main, 'model_adapter', model)
                await main._dispatch_channel_payload(state.adapter.account_id, payload('legacy-' + str(number), image=False), source_adapter=state.adapter)
                assert model.finished
                counts.append(len(list((state.roles.workspace_path(state.role.id) / 'attachments').iterdir())))
            assert counts[0] == counts[1], 'Same content must reuse its internal sending reference across ordinary turns'
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    sends = [params for operation, params in state.operations if operation == 'send_group_msg'
             and any(part['type'] == 'image' for part in params['message'])]
    assert len(sends) == 2
    assert state.downloads == []
    for send in sends:
        image = next(part for part in send['message'] if part['type'] == 'image')
        assert base64.b64decode(image['data']['file'].removeprefix('base64://')) == state.raw


@pytest.mark.parametrize('failure', ['ambiguous', 'path', 'old-attachment', 'other-role', 'download', 'corrupt'])
def test_invalid_current_sources_never_create_or_confirm_a_sticker(meme_turn, monkeypatch, failure):
    from backend.app.chat_attachments import save_attachment
    state = meme_turn
    state.raw = image_bytes('PNG')
    arguments = {'category': 'happy', 'description': '不可伪造的收录'}
    request = payload('invalid-' + failure)
    if failure == 'ambiguous':
        request['message'].append({'type': 'image', 'data': {'file': 'another.png', 'url': 'https://qq.test/image'}})
    elif failure in {'path', 'old-attachment', 'other-role'}:
        role = state.role
        if failure == 'other-role':
            role = state.roles.create(RoleInput(name='另一角色', profile=RoleProfile(profile='不能泄露')))
        old = save_attachment(state.roles.workspace_path(role.id), 'old.png', 'image/png', state.raw)
        arguments['attachment_id'] = old['path'] if failure == 'path' else old['id']
    elif failure == 'download':
        state.download_error = True
    else:
        state.raw = b'\x89PNG\r\n\x1a\ncorrupt-image-content'

    class Model:
        calls = 0
        finished = False
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            assert 'chat_meme_save' in {tool['function']['name'] for tool in tools}
            if self.calls == 1:
                yield ModelToolCall('invalid-save', 'chat_meme_save', arguments)
            else:
                result = next(item for item in reversed(messages) if item['role'] == 'tool')
                assert json.loads(result['content'])['status'] == 'failed'
                self.finished = True
                yield ModelTextDelta('收录失败，没有保存表情。')

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, request, source_adapter=state.adapter)
            assert model.finished
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert list((state.root / 'happy').iterdir()) == []
    assert json.loads((state.root / 'manifest.json').read_text(encoding='utf-8')) == state.manifest
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
    results = [message for message in state.sessions.list_messages(thread.session_key) if message.messageType == 'tool_result']
    assert len(results) == 1 and results[0].isError
    assert not any(part['type'] == 'image' for operation, params in state.operations
                   if operation == 'send_group_msg' for part in params['message'])


def _assert_save_cannot_pick_image(state, monkeypatch, request, *, model_required=True):
    class Model:
        calls = 0
        finished = False
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                yield ModelToolCall('unsafe-selection', 'chat_meme_save', {
                    'category': 'happy', 'description': '不得猜测图片来源',
                })
            else:
                result = next(item for item in reversed(messages) if item['role'] == 'tool')
                assert json.loads(result['content'])['status'] == 'failed'
                self.finished = True
                yield ModelTextDelta('不能确定获准图片，没有收录。')

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, request, source_adapter=state.adapter)
            if model_required or model.calls:
                assert model.finished, 'An admitted model turn must receive an actual failed save result'
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert list((state.root / 'happy').iterdir()) == []
    assert json.loads((state.root / 'manifest.json').read_text(encoding='utf-8')) == state.manifest
    assert not any(part['type'] == 'image' for operation, params in state.operations
                   if operation == 'send_group_msg' for part in params['message'])


def test_partial_download_failure_does_not_make_two_source_images_unambiguous(meme_turn, monkeypatch):
    state = meme_turn
    state.raw = image_bytes('PNG')
    state.download_failures = {'https://qq.test/image'}
    request = payload('partial-download')
    request['message'].append({'type': 'image', 'data': {'file': 'second.png', 'url': 'https://qq.test/second-image'}})
    _assert_save_cannot_pick_image(state, monkeypatch, request)
    assert state.downloads == ['https://qq.test/image', 'https://qq.test/second-image']
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
    inbound = next(message for message in state.sessions.list_messages(thread.session_key) if message.role == 'user')
    assert len(inbound.metadata['attachments']) == 1, 'Fixture must persist one genuine image while preserving original ambiguity'


@pytest.mark.parametrize('quoted_only', [False, True], ids=['at-with-quote', 'quote-only'])
@pytest.mark.parametrize('provenance', ['missing-group', 'private-message'])
def test_unverified_or_private_quote_cannot_supply_group_sticker(meme_turn, monkeypatch, quoted_only, provenance):
    state = meme_turn
    state.raw = image_bytes('PNG')
    if provenance == 'missing-group':
        state.quote_group = None
    else:
        # A contradictory private marker must be rejected even if a group_id
        # field appears alongside it in a malformed get_msg response.
        state.quote_type = 'private'
    request = payload('quote-provenance', quoted=quoted_only, image=False)
    if not quoted_only:
        request['message'].append({'type': 'reply', 'data': {'id': 'quoted-message'}})
    _assert_save_cannot_pick_image(state, monkeypatch, request, model_required=not quoted_only)


def test_current_attachment_replaced_after_turn_freeze_cannot_be_saved(meme_turn, monkeypatch):
    state = meme_turn
    state.raw = image_bytes('PNG')
    replacement = io.BytesIO()
    Image.new('RGB', (8, 7), (254, 0, 0)).save(replacement, format='PNG')
    changed = replacement.getvalue()
    assert changed != state.raw and len(changed) == len(state.raw), 'Both fixtures must be valid equal-size PNG originals'

    class Model:
        calls = 0
        finished = False
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            if self.calls == 1:
                thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
                inbound = next(message for message in state.sessions.list_messages(thread.session_key) if message.role == 'user')
                Path(inbound.metadata['attachments'][0]['path']).write_bytes(changed)
                yield ModelToolCall('replaced-save', 'chat_meme_save', {
                    'category': 'happy', 'description': '不得收录替换后的同路径图片',
                })
            else:
                result = next(item for item in reversed(messages) if item['role'] == 'tool')
                assert json.loads(result['content'])['status'] == 'failed'
                self.finished = True
                yield ModelTextDelta('原件已变化，没有收录。')

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, payload('replaced-source'), source_adapter=state.adapter)
            assert model.finished
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert list((state.root / 'happy').iterdir()) == []


def test_rejected_sticker_is_recorded_failed_and_same_candidate_retry_does_not_dispatch(meme_turn, monkeypatch):
    state = meme_turn
    state.raw, state.reject_image = image_bytes('PNG'), True
    (state.root / 'happy' / 'reject.png').write_bytes(state.raw)

    class Model:
        calls = 0
        candidate = None
        finished = False
        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            results = {item['tool_call_id']: item['content'] for item in messages if item['role'] == 'tool'}
            if self.calls == 1:
                yield ModelToolCall('list-rejected', 'chat_meme_list', {'query': '开心'})
            elif self.calls == 2:
                candidates = json.loads(results['list-rejected'])['candidates']
                assert len(candidates) == 1
                self.candidate = candidates[0]['id']
                yield ModelToolCall('send-rejected', 'chat_meme_send', {'candidate_id': self.candidate})
            elif self.calls == 3:
                assert json.loads(results['send-rejected'])['status'] == 'failed'
                yield ModelToolCall('retry-rejected', 'chat_meme_send', {'candidate_id': self.candidate})
            else:
                first, repeated = json.loads(results['send-rejected']), json.loads(results['retry-rejected'])
                assert repeated == first and first['status'] == 'failed'
                self.finished = True
                yield ModelTextDelta('平台拒绝了图片，未送达；没有重复发送。')

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id, payload('rejected-sticker', image=False), source_adapter=state.adapter)
            assert model.finished
        finally:
            if main._proactive is not None:
                await main._proactive.close()
    asyncio.run(asyncio.wait_for(scenario(), 20))
    image_sends = [params for operation, params in state.operations if operation == 'send_group_msg'
                   and any(part['type'] == 'image' for part in params['message'])]
    assert len(image_sends) == 1
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
    messages = state.sessions.list_messages(thread.session_key)
    outbound = [message for message in messages if message.role == 'assistant' and message.metadata.get('attachments')]
    assert len(outbound) == 1 and outbound[0].metadata['deliveryStatus'] == 'failed'
    results = [message for message in messages if message.messageType == 'tool_result'
               and message.toolCallId in {'send-rejected', 'retry-rejected'}]
    assert len(results) == 2 and all(message.isError for message in results)
    attempts = [attempt for attempt in state.channels.list_deliveries(state.role.id) if attempt['sourceKind'] == 'chat_meme']
    assert len(attempts) == 1 and attempts[0]['status'] == 'failed'
    assert not any(attempt['status'] == 'sent' for attempt in attempts), 'A failed sticker must not count as successful use'


def test_running_sticker_tool_cannot_send_after_same_adapter_socket_reconnect(meme_turn, monkeypatch):
    state = meme_turn
    state.raw = image_bytes('PNG')
    (state.root / 'happy' / 'before-reconnect.png').write_bytes(state.raw)
    transport = state.adapter._transport
    transport.connection_generation = 'socket-before'

    class Model:
        calls = 0
        finished = False

        async def stream_structured_messages(self, messages, configuration, tools):
            self.calls += 1
            results = {item['tool_call_id']: item['content'] for item in messages if item['role'] == 'tool'}
            if self.calls == 1:
                yield ModelToolCall('list-before-reconnect', 'chat_meme_list', {'query': '开心'})
            elif self.calls == 2:
                candidates = json.loads(results['list-before-reconnect'])['candidates']
                assert len(candidates) == 1
                # A real transport reconnect changes its epoch without replacing
                # the adapter or invalidating the ordinary running AgentRun.
                transport.connection_generation = 'socket-after'
                assert main.channel_host.adapter(state.adapter.account_id) is state.adapter
                yield ModelToolCall('send-after-reconnect', 'chat_meme_send', {'candidate_id': candidates[0]['id']})
            else:
                assert json.loads(results['send-after-reconnect'])['status'] == 'failed'
                self.finished = True
                yield ModelTextDelta('连接已变化，没有发送旧回合表情。')

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)

    async def scenario():
        try:
            await main._dispatch_channel_payload(state.adapter.account_id,
                payload('sticker-reconnect', image=False), source_adapter=state.adapter)
            assert model.finished
        finally:
            if main._proactive is not None:
                await main._proactive.close()

    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert not any(operation == 'send_group_msg' for operation, _ in state.operations)
    thread = state.channels.ensure_channel_thread(state.role.id, 'qq', state.adapter.account_id, '100', 'group')
    result = next(message for message in state.sessions.list_messages(thread.session_key)
                  if message.messageType == 'tool_result' and message.toolCallId == 'send-after-reconnect')
    assert result.isError
