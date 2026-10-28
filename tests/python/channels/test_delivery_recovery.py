import asyncio
import json

import httpx
import pytest

from backend.app.channels.delivery import OutboundScheduler
from backend.app.channels.feishu import FeishuAdapter, FeishuConfig
from backend.app.channels.models import DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.transports import FeishuWebhookTransport


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []
        self.after_wait = lambda: None

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        self.after_wait()

    def scheduler(self):
        return OutboundScheduler(clock=lambda: self.now, sleep=self.sleep)


def deliver(responses, *, clock=None, deadline=None, authorized=lambda: True):
    clock = clock or Clock()
    requests, snapshots = [], []
    async def scenario():
        def handle(request):
            if '/auth/' in request.url.path:
                return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic', 'expire': 7200})
            requests.append(request)
            item = responses[len(requests) - 1]
            if isinstance(item, Exception):
                raise item
            status, body, headers = item
            return httpx.Response(status, json=body, headers=headers)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            transport = FeishuWebhookTransport('app', 'synthetic', http_client=client)
            adapter = FeishuAdapter(FeishuConfig('account', 'app'), transport=transport, scheduler=clock.scheduler())
            target = target_from_chat(account_id='account', chat_id='original-chat', kind=TargetKind.PRIVATE)
            adapter.remember_target(target)
            return await adapter.send(OutboundMessage(target, '**内容**', reply_to_message_id='recalled',
                delivery_deadline=deadline, authorize_dispatch=authorized, record_delivery=snapshots.append))
    return asyncio.run(scenario()), requests, snapshots, clock


def test_feishu_wire_recovery_shares_four_attempts_and_same_target():
    result, requests, snapshots, clock = deliver([
        (429, {'code': 99991400}, {'x-ogw-ratelimit-reset': '3'}),
        (400, {'code': 230011, 'msg': 'The message is recalled.'}, {}),
        (400, {'code': 230099, 'msg': 'Failed to create card content, code: 200621'}, {}),
        (200, {'code': 0, 'data': {'message_id': 'actual-receipt'}}, {}),
    ])
    assert result.status is DeliveryStatus.SENT
    assert clock.waits == [3]
    bodies = [json.loads(request.content) for request in requests]
    assert [request.url.path for request in requests[:2]] == ['/open-apis/im/v1/messages/recalled/reply'] * 2
    assert [body['receive_id'] for body in bodies[2:]] == ['original-chat'] * 2
    assert bodies[-1]['msg_type'] == 'text'
    assert result.raw['parts'][0]['attempts'] == 4
    assert result.raw['parts'][0]['withoutReply'] and result.raw['parts'][0]['plainText']
    assert snapshots[-1]['parts'][0]['platformMessageId'] == 'actual-receipt'


@pytest.mark.parametrize('status,body,expected', [
    (403, {'code': 230013}, DeliveryStatus.FAILED),
    (400, {'code': 230099, 'msg': 'code: 200381'}, DeliveryStatus.FAILED),
    (400, {'code': 230019}, DeliveryStatus.FAILED),
    (500, {'code': 230020}, DeliveryStatus.UNCERTAIN),
    (200, {'code': 0}, DeliveryStatus.UNCERTAIN),
])
def test_feishu_permission_topic_unknown_and_missing_receipts_never_fallback(status, body, expected):
    result, requests, _, _ = deliver([(status, body, {})])
    assert result.status is expected
    assert len(requests) == 1


def test_wait_respects_original_deadline_and_rechecks_revocation():
    rate_limit = (429, {'code': 99991400}, {'x-ogw-ratelimit-reset': '3'})
    result, requests, _, clock = deliver([rate_limit], deadline=102)
    assert result.status is DeliveryStatus.FAILED and result.error == 'delivery_deadline'
    assert len(requests) == 1 and not clock.waits
    clock = Clock()
    allowed = [True]
    clock.after_wait = lambda: allowed.__setitem__(0, False)
    result, requests, _, _ = deliver([rate_limit], clock=clock, authorized=lambda: allowed[0])
    assert result.status is DeliveryStatus.FAILED and result.error == 'source_revoked'
    assert len(requests) == 1
