from __future__ import annotations

import asyncio
import json

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.agent_runtime.tools import ToolContext
from backend.app.channels.models import DeliveryResult, DeliveryStatus, Target, TargetKind
from backend.app.channels.tools import ChannelSendTool


class _Host:
    def __init__(self) -> None:
        self.call: dict[str, object] | None = None

    async def account_send(self, role_id, account_id, target_id, content, **kwargs):
        self.call = {
            "role_id": role_id,
            "account_id": account_id,
            "target_id": target_id,
            "content": content,
            **kwargs,
        }
        target = Target(target_id, TargetKind.PRIVATE, "Owner", account_id, "owner-chat")
        return DeliveryResult(DeliveryStatus.SENT, target, "message-1", attempt_id="attempt-1")


def test_account_send_passes_attachment_ids_to_role_scoped_host() -> None:
    host = _Host()
    tool = ChannelSendTool(host, is_owner=True, source_thread_id="thread-1")
    context = ToolContext("role-1", "thread-1", "run-1", CancellationToken())
    attachment_id = "a" * 32

    result = asyncio.run(tool.execute({
        "account_id": "qq-1",
        "target_id": "target:v1:opaque-owner-target",
        "content": "看一下这个文件",
        "attachments": [attachment_id],
    }, context))

    authorize = host.call.pop('authorize_source')
    assert authorize()
    context.signal.cancel()
    assert not authorize()
    assert host.call.pop('delivery_deadline') is None
    assert host.call == {
        "role_id": "role-1",
        "account_id": "qq-1",
        "target_id": "target:v1:opaque-owner-target",
        "content": "看一下这个文件",
        "attachment_ids": (attachment_id,),
        "mention_ids": (),
        "message_thread_id": None,
        "actor": "owner",
        "source_thread_id": "thread-1",
        "source_session_key": "thread-1",
        "source_run_id": "run-1",
        "source_tool_call_id": None,
        "source_kind": "account_send",
    }
    assert json.loads(result.content)["status"] == "sent"
    assert ChannelSendTool.definition.input_schema["properties"]["attachments"]["items"]["pattern"] == "^[0-9a-f]{32}$"


def test_account_send_rejects_empty_payload_before_transport() -> None:
    host = _Host()
    tool = ChannelSendTool(host, is_owner=True, source_thread_id="thread-1")
    context = ToolContext("role-1", "thread-1", "run-1", CancellationToken())

    result = asyncio.run(tool.execute({"account_id": "qq-1", "target_id": "target"}, context))

    assert result.is_error
    assert host.call is None


def test_media_paths_and_public_urls_use_owned_bytes_in_real_qq_payload(tmp_path, monkeypatch):
    import base64
    from io import BytesIO
    from PIL import Image
    import httpx
    from backend.app import network_tools
    from backend.app.chat_attachments import resolve_attachment
    from backend.app.channels import ChannelHost, NapCatAdapter, NapCatConfig, target_from_chat
    from test_host import make_store

    root = tmp_path / 'role'
    root.mkdir()
    image = BytesIO()
    Image.new('RGB', (2, 2), '#123456').save(image, format='PNG')
    raw = image.getvalue()
    (root / 'output.png').write_bytes(raw)
    store = make_store(tmp_path)
    account = store.register_channel_account('role', 'qq', account_id='qq', enabled=True)
    destination = target_from_chat(account_id='qq', chat_id='456', kind=TargetKind.PRIVATE)
    store.remember_targets('role', [destination])
    calls = []
    async def transport(operation, params):
        calls.append((operation, params))
        return {'message_id': 100 + len(calls)}
    adapter = NapCatAdapter(NapCatConfig(account_id='qq'), transport=transport)
    adapter.remember_target(destination)
    host = ChannelHost(store, attachment_resolver=lambda role, identifier: resolve_attachment(root, identifier) if role == 'role' else None)
    host.register(adapter)
    original_download = network_tools._download
    async def resolve(_host, _port):
        return ['8.8.8.8']
    def image_response(request):
        assert request.url.host == '8.8.8.8' and request.headers['host'] == 'example.com'
        return httpx.Response(200, content=raw, headers={'content-type': 'image/png'})
    async def download(config, _resolver, _transport, url):
        return await original_download(config, resolve, httpx.MockTransport(image_response), url)
    monkeypatch.setattr(network_tools, '_download', download)

    async def run():
        owner = ChannelSendTool(host, is_owner=True, source_thread_id=None, workspace=lambda: root)
        context = ToolContext('role', 'role:role', 'run', CancellationToken(), 'send')
        result = await owner.execute({'account_id': 'qq', 'target_id': destination.id, 'media': ['output.png']}, context)
        assert not result.is_error
        source = store.ensure_channel_thread('role', 'qq', account.account_id, '987', 'private')
        code = store.create_pairing_code('role', 'qq')
        assert store.consume_pairing_code(code, role_id='role', channel='qq', scope='platform',
            platform_user_id='456', account_id='qq', chat_id='456')
        guest = ChannelSendTool(host, is_owner=False, source_thread_id=source.thread_id,
            source_account_id='qq', workspace=lambda: root)
        guest_context = ToolContext('role', source.session_key, 'guest', CancellationToken(), 'send')
        assert (await guest.execute({'target_id': 'owner', 'media': ['output.png']}, guest_context)).is_error
        result = await guest.execute({'target_id': 'owner', 'media': ['https://example.com/remote.png']}, guest_context)
        assert not result.is_error
        remote_id = store.sessions.list_messages(store.get_thread_by_chat('role', 'qq', '456').session_key)[-1].metadata['attachments'][0]['id']
        # Just knowing a role-owned ID is not an external source grant.
        assert (await guest.execute({'target_id': 'owner', 'media': [remote_id]}, guest_context)).is_error
    asyncio.run(run())
    assert len(calls) == 2
    for operation, payload in calls:
        assert operation == 'send_private_msg'
        assert payload['user_id'] == 456
        encoded = payload['message'][0]['data']['file']
        assert base64.b64decode(encoded.removeprefix('base64://')) == raw
    thread = store.get_thread_by_chat('role', 'qq', '456')
    messages = store.sessions.list_messages(thread.session_key)
    assert len(messages) == 2
    assert all(item.metadata['deliveryStatus'] == 'sent' for item in messages)
    assert all(item.metadata['attachments'][0]['id'] for item in messages)


def test_guest_can_choose_another_owned_account_but_only_the_bound_owner() -> None:
    host = _Host()
    tool = ChannelSendTool(host, is_owner=False, source_thread_id='source', source_account_id='qq-source')
    context = ToolContext('role', 'thread:source', 'run', CancellationToken())
    assert 'account_id' in tool.definition.input_schema['properties']
    result = asyncio.run(tool.execute({'account_id': 'telegram-owner', 'target_id': 'owner', 'content': '请主人看看'}, context))
    assert not result.is_error
    assert host.call['account_id'] == 'telegram-owner'
    assert host.call['actor'] == 'external'
    assert host.call['source_thread_id'] == 'source'
    host.call = None
    denied = asyncio.run(tool.execute({'account_id': 'telegram-owner', 'target_id': 'third-party', 'content': '不发'}, context))
    assert denied.is_error and host.call is None
