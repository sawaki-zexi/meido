"""Original Drift grants and immutable bytes reach the real channel transport."""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelToolCallBlock
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.chat_attachments import resolve_attachment
from backend.app.proactive_runtime.identity import AutonomyOrigin
from test_proactive_channel_delivery import channel_for
from test_proactive_drift_delivery import prepare, settled
from test_proactive_targets_api import api, shell_api, platform, save_external  # noqa: F401

pytestmark = pytest.mark.usefixtures('local_platform_only')
ORIGINAL = b'\x89PNG\r\n\x1a\noriginal-image'
CHANGED = b'\x89PNG\r\n\x1a\nreplaced-image'


def test_original_drift_origin_rejects_run_external_ids_and_sends_frozen_image_after_lease(api, monkeypatch, platform):
    client, _, sessions, _ = api
    role, path, now = prepare(client, monkeypatch, extra_tools=('message_push',))

    def upload(owner, name):
        response = client.post(f'/api/roles/{owner}/attachments', content=ORIGINAL,
            headers={'x-attachment-filename': name + '.png', 'content-type': 'image/png'})
        assert response.status_code == 200, response.text
        return response.json()

    own, ungranted = upload(role, 'original'), upload(role, 'ungranted')
    foreign = client.post('/api/roles', json={'name': 'other media owner', 'profile': {'profile': 'private'}}).json()['role']['id']
    other = upload(foreign, 'foreign')
    config = client.get(path).json()['config']
    response = client.put(path, json={'expectedRevision': config['revision'],
        'config': {**config, 'driftAttachmentIds': [own['id']]}})
    assert response.status_code == 200, response.text
    account = channel_for(main.multichannel_store, role, platform[1])
    adapter = NapCatAdapter(NapCatConfig(account.account_id), transport=platform[1])
    main.channel_host.register(adapter)
    config = response.json()['config']
    _, selected = save_external(client, path, account, config, expected_revision=config['revision'])
    app, channels = main._proactive, main.multichannel_store
    # The shared process fixture intentionally omits media. Install the same
    # real role-scoped resolver used by production main's ChannelHost.
    monkeypatch.setattr(main.channel_host, 'attachment_resolver',
        lambda owner, identifier: resolve_attachment(main.store.workspace_path(owner), identifier))
    metadata = json.loads((main.store.workspace_path(role) / 'attachments' / (own['id'] + '.json')).read_text(encoding='utf-8'))
    image_path = Path(metadata['path'])
    original_send = []

    class Model:
        requests = 0

        async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
            self.requests += 1
            if self.requests == 1:
                name, arguments = 'activate_skill', {'skill_id': 'reading-notes'}
            elif self.requests in {2, 3, 4}:
                if self.requests > 2:
                    assert messages[-1].is_error
                if self.requests < 4:
                    assert channels.list_deliveries(role) == [] and platform[0]['sent'] == []
                identifier = {2: other['id'], 3: ungranted['id'], 4: own['id']}[self.requests]
                name, arguments = 'message_push', {'message': 'original frozen autonomous image', 'mood': '', 'thought': '',
                    'attachment_ids': [identifier], 'account_id': account.account_id, 'target_id': selected['id']}
            else:
                assert self.requests == 5 and json.loads(messages[-1].content[0].text)['status'] == 'sent'
                name, arguments = 'finish_drift', {'skill_used': 'reading-notes', 'one_line': '真实原图片已投递',
                    'next_action': '保持单次机会', 'message_result': 'sent'}
            yield ModelStreamDone(ModelAssistantMessage(content=(ModelToolCallBlock(f'channel-media-{self.requests}', name, arguments),)))

    model = Model()
    monkeypatch.setattr(main, 'model_adapter', model)
    # Reuse the actual selected adapter, so the approved generation stays fixed.
    send = adapter.send

    async def mutate_after_authorization(outbound):
        assert model.requests == 4 and len(outbound.attachments) == 1
        assert outbound.attachments[0].frozen_data == ORIGINAL
        assert image_path.read_bytes() == ORIGINAL
        attempts = channels.list_deliveries(role)
        assert len(attempts) == 1 and attempts[0]['status'] == 'pending'
        with app._transaction() as db:
            run = db.execute('SELECT origin FROM autonomy_runs WHERE tick_id=?', (attempts[0]['sourceRunId'],)).fetchone()
            origin = AutonomyOrigin.restore(run[0])
            assert app._authorized_in(db, origin)
            assert db.execute('SELECT dispatched FROM autonomy_deliveries WHERE intent_id=?',
                (attempts[0]['sourceToolCallId'],)).fetchone()[0] == 1
        # ChannelHost reached the real adapter after taking its send lease and
        # checking the original AutonomyOrigin and run-scoped attachment grant.
        image_path.write_bytes(CHANGED)
        original_send.append(outbound)
        return await send(outbound)

    monkeypatch.setattr(adapter, 'send', mutate_after_authorization)
    try:
        now[0] += 1800
        final = asyncio.run(settled(app, role))
        assert final['runs'][0]['status'] == 'finished_sent' and model.requests == 5, json.dumps({
            'runs': final['runs'], 'deliveries': final['deliveries'], 'requests': model.requests}, ensure_ascii=True)
        assert len(platform[0]['sent']) == 1 and len(original_send) == 1
        images = [item for item in platform[0]['sent'][0]['message'] if item['type'] == 'image']
        assert len(images) == 1
        assert base64.b64decode(images[0]['data']['file'].removeprefix('base64://')) == ORIGINAL
        assert image_path.read_bytes() == CHANGED
        delivery = final['deliveries'][0]
        assert delivery['status'] == 'sent' and delivery['platformMessageId'] == '8301'
        assert delivery['target']['ref'] == selected['targetRef'] and not delivery['projectionPending']
        thread = channels.list_threads(role)[0]
        messages = channels.list_messages(role, thread.thread_id)[1]
        assert len(messages) == 1 and messages[0].metadata['attachments'] == [own]
        assert messages[0].metadata['autonomyTickId'] == final['runs'][0]['id']
        assert all('path' not in item for item in messages[0].metadata['attachments'])
        assert not any(item.content == 'original frozen autonomous image' for item in sessions.list_messages(f'role:{role}'))
        with app._transaction() as db:
            claim = db.execute('SELECT draft FROM autonomy_deliveries WHERE intent_id=?', (delivery['intentId'],)).fetchone()[0]
            assert str(image_path) not in claim and 'frozen_data' not in claim and 'base64://' not in claim
    finally:
        asyncio.run(app.close())
        asyncio.run(main.channel_host.stop(account.account_id))
