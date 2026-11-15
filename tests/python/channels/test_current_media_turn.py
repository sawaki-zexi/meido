"""Actual QQ intake -> provider -> public API, with controlled external I/O."""
import asyncio
import json
import hashlib

import pytest

from test_meme_turn import meme_turn, image_bytes
from test_qq_image_receiving import controlled_model, dispatch_and_read, event_for, add_image, images


@pytest.mark.parametrize('kind,data,expected', [
    ('face', {'id': '14'}, ['face', 'id=14']),
    ('mface', {'emoji_id': 'ABC123', 'emoji_package_id': '9', 'key': 'private-key', 'url': 'https://secret.test'},
        ['mface', 'emoji_id=ABC123', 'emoji_package_id=9']),
    ('face', {'id': '<script>alert(1)</script>'}, ['face', 'id=无效']),
    ('mface', {'emoji_id': 'x' * 4000, 'summary': '注入文本'}, ['mface', 'emoji_id=无效']),
])
def test_native_segments_reach_model_and_public_history_without_claimed_pixels(meme_turn, monkeypatch, kind, data, expected):
    state, requests = meme_turn, []
    controlled_model(monkeypatch, requests)
    event = event_for('native-current', '22')
    event['message'].append({'type': kind, 'data': data})
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    for marker in [*expected, '像素读取不支持', '原生发送不支持']:
        assert marker in current['content'] and marker in str(requests)
    public = json.dumps(result, ensure_ascii=False)
    for forbidden in ['<script>', 'private-key', 'https://secret.test', '注入文本', 'x' * 500]:
        assert forbidden not in public + str(requests)
    assert images(requests) == 0 and not state.downloads


def test_native_segment_count_is_bounded(meme_turn, monkeypatch):
    state, requests = meme_turn, []
    controlled_model(monkeypatch, requests)
    event = event_for('native-many', '22', text='')
    event['message'].extend({'type': 'face', 'data': {'id': str(index)}} for index in range(200))
    _, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert current['content'].count('原生发送不支持') == 16
    assert '其余原生表情已省略' in current['content']


@pytest.mark.parametrize('quoted', [False, True])
@pytest.mark.parametrize('fmt', ['PNG', 'GIF'])
def test_current_and_reference_pixels_have_run_receipts_without_collection(meme_turn, monkeypatch, quoted, fmt):
    state, requests = meme_turn, []
    state.raw = image_bytes(fmt)
    state.extension, state.media_type = fmt.lower(), 'image/' + fmt.lower()
    controlled_model(monkeypatch, requests)
    event = event_for('visual-current', '22', quoted=quoted)
    if not quoted:
        add_image(event, 'https://qq.test/image')
        event['message'][-1]['data']['file'] = 'current.' + state.extension
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    response = next(item for item in result['messages'] if item['role'] == 'assistant')
    run = state.sessions.get_run(response['runId'])
    receipts = run.modelSnapshot['mediaRequests']
    assert len(receipts) == len(requests) == 1
    records = receipts[0]['images']
    assert len(records) == images(requests) == (2 if fmt == 'GIF' else 1)
    attachments = current['metadata']['quotedAttachments' if quoted else 'attachments']
    assert all(item['sourceId'] == attachments[0]['id'] for item in records)
    assert all(item['sourceKind'] == ('reference' if quoted else 'message') for item in records)
    assert all(item['sourceMessageIds'] == [current['id']] for item in records)
    assert all(item['contentSha256'] == hashlib.sha256(state.raw).hexdigest() for item in records)
    assert all(item['status'] == 'pixels_submitted' for item in records)
    assert not list((state.root / 'happy').iterdir())
    assert 'base64' not in json.dumps(receipts) and 'https://' not in json.dumps(receipts)


def test_verified_quoted_native_identity_reaches_model_and_public_history(meme_turn, monkeypatch):
    state, requests = meme_turn, []
    controlled_model(monkeypatch, requests)
    original = state.adapter._transport
    async def transport(operation, params):
        result = await original(operation, params)
        if operation == 'get_msg':
            result['data']['message'] = [{'type': 'mface', 'data': {
                'emoji_id': 'QUOTE1', 'emoji_package_id': '12', 'url': 'https://secret.test', 'key': 'secret-key'}}]
        return result
    monkeypatch.setattr(state.adapter, '_transport', transport)
    event = event_for('native-reference', '22', quoted=True)
    result, current = asyncio.run(asyncio.wait_for(dispatch_and_read(state, event), 10))
    assert 'emoji_id=QUOTE1' in str(requests)
    assert 'emoji_id=QUOTE1' in current['metadata']['replyToContent']
    assert '原生发送不支持' in json.dumps(result, ensure_ascii=False)
    assert not state.downloads and images(requests) == 0
