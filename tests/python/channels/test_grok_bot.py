import asyncio
import json
import shutil

import httpx

from backend.app.channels.models import OutboundMessage, DeliveryStatus


def test_fixed_bot_webhook_and_real_local_client_share_one_bounded_exchange(tmp_path):
    from backend.app.channels.grok.adapter import GrokAdapter

    async def scenario():
        posts, received = [], []
        entered, release = asyncio.Event(), asyncio.Event()

        async def remote(request):
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={'task_id': 'not-a-message-id'})

        async def inbound(payload):
            received.append(payload)
            entered.set()
            await release.wait()

        settings = {'endpoint': 'https://example.invalid/routine', 'peerId': 'a' * 32, 'maxMessages': 2}
        adapter = GrokAdapter('grok-test', 'role-test', settings, str(tmp_path / 'channels.db'),
            webhook_key='webhook-test', callback_token='callback-test-secret-value-1234567890',
            http_transport=httpx.MockTransport(remote))
        adapter.set_event_handler(inbound)
        await adapter.start()
        try:
            assert not posts
            targets = await adapter.list_targets()
            assert len(targets) == 1
            sent = await adapter.send(OutboundMessage(targets[0], '你好，Bot'))
            assert sent.status is DeliveryStatus.SENT and sent.platform_message_id is None
            assert sent.raw['receiptKind'] == 'transport_acceptance'
            assert posts[0]['protocol'] == 'meido-grok/1'
            assert 'webhook-test' not in json.dumps(posts)
            payload = {'message_id': 'bot-1', 'text': '你好，角色', 'reply_to': posts[0]['message_id']}
            shell = shutil.which('powershell.exe') or shutil.which('pwsh')
            assert shell, 'The client contract requires PowerShell (available on Windows and CI).'
            process = await asyncio.create_subprocess_exec(shell, '-NoProfile', '-NonInteractive',
                '-File', posts[0]['callback_client'], '-Connection', posts[0]['connection_file'],
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(payload).encode()), 20)
            assert process.returncode == 0, stderr.decode(errors='replace')
            assert json.loads(stdout)['status'] == 'accepted'
            await asyncio.wait_for(entered.wait(), 3)
            assert len(received) == 1  # HTTP receipt arrived while the role is still waiting.
            connection = json.loads(adapter.client_file.read_text())
            async with httpx.AsyncClient(trust_env=False) as client:
                url = connection['callback_url'] + 'messages'
                headers = {'Authorization': 'Bearer ' + connection['token']}
                unauthenticated = await client.post(url, json={'message_id': 'ignored', 'text': 'ignored'})
                assert unauthenticated.status_code == 401
                unknown_parent = await client.post(url, headers=headers,
                    json={'message_id': 'unknown-parent', 'text': 'reply', 'reply_to': 'never-seen'})
                assert unknown_parent.status_code == 409
                duplicate = await client.post(url, headers=headers, json=payload)
                assert duplicate.status_code == 200 and duplicate.json()['status'] == 'duplicate'
                conflict = await client.post(url, headers=headers, json={**payload, 'text': 'changed'})
                assert conflict.status_code == 409
            exhausted = await adapter.send(OutboundMessage(targets[0], '第三条', reply_to_message_id='bot-1'))
            assert exhausted.status is DeliveryStatus.FAILED and len(posts) == 1
            release.set()
        finally:
            release.set()
            await adapter.stop()
        assert not adapter.client_file.exists()
        # A new adapter uses the original persisted exchange budget.
        again = GrokAdapter('grok-test', 'role-test', settings, str(tmp_path / 'channels.db'),
            webhook_key='webhook-test', callback_token='different-callback-secret-1234567890',
            http_transport=httpx.MockTransport(remote))
        await again.start()
        try:
            exhausted = await again.send(OutboundMessage((await again.list_targets())[0], '仍是第三条', reply_to_message_id='bot-1'))
            assert exhausted.status is DeliveryStatus.FAILED and len(posts) == 1
        finally:
            await again.stop()

    asyncio.run(scenario())


def test_unknown_grok_submission_is_not_replayed(tmp_path):
    from backend.app.channels.grok.adapter import GrokAdapter

    async def scenario():
        requests = []
        async def remote(request):
            requests.append(request)
            raise httpx.ReadTimeout('response lost')
        adapter = GrokAdapter('grok-test', 'role-test',
            {'endpoint': 'https://example.invalid/bot', 'peerId': 'a' * 32, 'maxMessages': 12},
            str(tmp_path / 'channels.db'), webhook_key='private-test', callback_token='private-callback-test',
            http_transport=httpx.MockTransport(remote))
        await adapter.start()
        try:
            message = OutboundMessage((await adapter.list_targets())[0], 'hello', metadata={'deliveryAttemptId': 'one-attempt'})
            first = await adapter.send(message)
            second = await adapter.send(message)
            assert first.status is second.status is DeliveryStatus.UNCERTAIN
            assert first.platform_message_id is second.platform_message_id is None
            assert len(requests) == 1
        finally:
            await adapter.stop()
    asyncio.run(scenario())
