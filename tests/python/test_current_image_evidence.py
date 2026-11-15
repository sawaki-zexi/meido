"""Request receipts prove submitted inputs, not model perception quality."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.app.agent_runtime import CancellationToken, MeidoProvider
from backend.app.agent_runtime.types import UserMessage
from backend.app.model_adapter import ModelTextDelta
from backend.app.models import RoleInput, RoleProfile
from backend.app.session_store import load_runtime_attachments

CORPUS = json.loads((Path(__file__).parents[1] / 'fixtures/current-media-quality.json').read_text(encoding='utf-8'))


def picture(frames=1):
    stream = io.BytesIO()
    images = [Image.new('RGB', (12, 9), (index * 37 % 256, 20, 60)) for index in range(frames)]
    images[0].save(stream, format='GIF' if frames > 1 else 'PNG',
        **({'save_all': True, 'append_images': images[1:], 'duration': 80} if frames > 1 else {}))
    return stream.getvalue()


def input_message(tmp_path, frames=1):
    raw = picture(frames)
    path = tmp_path / ('image.gif' if frames > 1 else 'image.png')
    path.write_bytes(raw)
    attachments = load_runtime_attachments({'attachments': [{'id': 'attachment-current', 'path': str(path),
        'name': path.name, 'mediaType': 'image/gif' if frames > 1 else 'image/png'}]})
    return UserMessage('看看图片', attachments, source_message_ids=('message-current',)), raw


class RecordingAdapter:
    def __init__(self, supports_images=True):
        self.capabilities = SimpleNamespace(supports_images=supports_images)
        self.requests = []

    async def stream_structured_messages(self, messages, configuration, tools):
        self.requests.append(messages)
        yield ModelTextDelta('收到')


def provider_for(adapter):
    role = RoleInput(name='视觉角色', profile=RoleProfile(profile='测试')).model_copy(update={'id': 'role-test'})
    return MeidoProvider(adapter, role, None, structured_without_tools=True)


async def send(provider, message, signal=None):
    return [event async for event in provider.stream_response(model='test', system='', messages=[message],
        tools=[], signal=signal or CancellationToken())]


def test_preparation_is_not_submission_and_each_request_has_its_own_receipt(tmp_path):
    message, raw = input_message(tmp_path)
    adapter = RecordingAdapter()
    provider = provider_for(adapter)
    receipts = []
    provider.media_request_observer = receipts.append
    provider.prepare_request('', [message], [])
    assert receipts == []
    asyncio.run(send(provider, message))
    asyncio.run(send(provider, message))
    assert len(receipts) == len(adapter.requests) == 2
    assert receipts[0]['requestId'] != receipts[1]['requestId']
    record = receipts[0]['images'][0]
    assert record['status'] == 'pixels_submitted'
    assert record['sourceId'] == 'attachment-current'
    assert record['sourceMessageIds'] == ['message-current']
    assert record['contentSha256'] == hashlib.sha256(raw).hexdigest()
    assert record['representationSha256'] == record['contentSha256']
    assert 'base64' not in json.dumps(receipts) and str(tmp_path) not in json.dumps(receipts)


def test_gif_request_reports_actual_sample_positions_and_keeps_original(tmp_path):
    message, raw = input_message(tmp_path, 7)
    provider = provider_for(RecordingAdapter())
    receipts = []
    provider.media_request_observer = receipts.append
    asyncio.run(send(provider, message))
    images = receipts[0]['images']
    assert len(images) == 3
    assert [item['frameIndex'] for item in images] == [0, 3, 6]
    assert all(item['frameCount'] == 7 and item['coverage'] == 'sampled' for item in images)
    assert all(item['contentSha256'] == hashlib.sha256(raw).hexdigest() for item in images)
    assert all(item['representationSha256'] != item['contentSha256'] for item in images)
    assert '未覆盖完整动画' in str(provider.adapter.requests)
    assert (tmp_path / 'image.gif').read_bytes() == raw


def test_text_only_model_reports_no_pixels_and_cancellation_has_no_receipt(tmp_path):
    message, _ = input_message(tmp_path)
    adapter = RecordingAdapter(False)
    provider = provider_for(adapter)
    receipts = []
    provider.media_request_observer = receipts.append
    asyncio.run(send(provider, message))
    assert receipts[0]['images'][0]['status'] == 'unsupported'
    assert 'image_url' not in str(adapter.requests) and '未提供像素' in str(adapter.requests)
    signal = CancellationToken()
    signal.cancel()
    with pytest.raises(RuntimeError, match='cancelled'):
        asyncio.run(send(provider, message, signal))
    assert len(receipts) == len(adapter.requests) == 1


@pytest.mark.parametrize('sample', CORPUS['samples'], ids=lambda sample: sample['id'])
def test_fixed_quality_corpus_read_input_subset(tmp_path, sample):
    from PIL import ImageDraw
    assert CORPUS['qualityStatus'] == 'quality-unverified' and len(CORPUS['samples']) >= 20
    frames = []
    for index in range(sample['frames']):
        image = Image.new('RGB', (128, 48), sample['color'])
        ImageDraw.Draw(image).text((4, 4), sample['text'] + (f' {index}' if sample['frames'] > 1 else ''), fill='black')
        frames.append(image)
    path = tmp_path / (sample['id'] + '.' + sample['format'].lower())
    frames[0].save(path, format=sample['format'], **({'save_all': True, 'append_images': frames[1:], 'duration': 80}
        if sample['frames'] > 1 else {}))
    media = 'image/' + sample['format'].lower()
    attachments = load_runtime_attachments({'attachments': [{'id': sample['id'], 'path': str(path), 'mediaType': media}]})
    message = UserMessage('描述已提供的图；不确定的内容请说明。', attachments, source_message_ids=(sample['id'],))
    provider = provider_for(RecordingAdapter())
    asyncio.run(send(provider, message))
    receipt = provider.last_media_request
    records = receipt['images']
    assert receipt['understanding'] == 'quality-unverified'
    assert len(records) == min(sample['frames'], 3)
    assert all(item['contentSha256'] == hashlib.sha256(path.read_bytes()).hexdigest() for item in records)
    assert all(item['status'] == 'pixels_submitted' for item in records)
    assert [item['frameIndex'] for item in records] == sorted({0, sample['frames'] // 2, sample['frames'] - 1})


def test_receipt_is_bounded_and_same_pixels_keep_distinct_source_identities(tmp_path):
    from dataclasses import replace
    message, _ = input_message(tmp_path)
    first = message.attachments[0]
    attachments = tuple(replace(first, image=replace(first.image, source=replace(first.image.source,
        source_id=f'image-{index}', source_kind='reference' if index % 2 else 'message',
        source_message_id=f'original-{index}'))) for index in range(80))
    provider = provider_for(RecordingAdapter())
    asyncio.run(send(provider, replace(message, attachments=attachments,
        source_message_ids=('https://private.test/' + 'x' * 4000,) * 20)))
    receipt = provider.last_media_request
    assert receipt['totalImages'] == receipt['submittedImages'] == 80 and receipt['omittedImages'] == 16
    assert len(receipt['images']) == 64
    assert [item['sourceId'] for item in receipt['images']] == [f'image-{index}' for index in range(64)]
    assert receipt['images'][1]['sourceKind'] == 'reference' and receipt['images'][1]['originalMessageId'] == 'original-1'
    assert len(receipt['images'][0]['sourceMessageIds']) == 8
    assert 'private.test' not in json.dumps(receipt)


@pytest.mark.parametrize('denial', ['revocation', 'snapshot', 'quota'])
def test_rejected_prepared_request_cannot_publish_old_evidence(tmp_path, denial):
    message, _ = input_message(tmp_path)
    provider = provider_for(RecordingAdapter())
    prepared = provider.prepare_request('', [message], [])
    receipts = []
    provider.media_request_observer = receipts.append
    def reject():
        raise PermissionError('source no longer current')
    async def quota(*args):
        reject()
    if denial == 'quota':
        provider.request_start = quota
    else:
        setattr(provider, 'request_guard' if denial == 'revocation' else 'snapshot_guard', reject)
    async def run():
        return [event async for event in provider.stream_prepared(prepared, system='', messages=[message], signal=CancellationToken())]
    asyncio.run(run())
    assert receipts == provider.adapter.requests == []


def test_source_replacement_changes_content_version_without_reusing_old_receipt(tmp_path):
    from dataclasses import replace
    message, original = input_message(tmp_path)
    provider = provider_for(RecordingAdapter())
    asyncio.run(send(provider, message))
    first = provider.last_media_request
    path = tmp_path / 'image.png'
    Image.new('RGB', (12, 9), 'yellow').save(path)
    loaded = load_runtime_attachments({'attachments': [{'id': 'attachment-current', 'path': str(path), 'mediaType': 'image/png'}]})
    asyncio.run(send(provider, replace(message, attachments=loaded)))
    second = provider.last_media_request
    assert first['requestId'] != second['requestId']
    assert first['images'][0]['contentSha256'] == hashlib.sha256(original).hexdigest()
    assert second['images'][0]['contentSha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first['images'][0]['contentSha256'] != second['images'][0]['contentSha256']


def test_real_openai_wire_pixels_match_receipt(tmp_path, monkeypatch):
    import httpx
    from backend.app.model_adapter import OpenAICompatibleAdapter
    from backend.app.models import ModelConfiguration
    message, _ = input_message(tmp_path, 7)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"收到"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            headers={'content-type': 'text/event-stream'})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    provider = provider_for(OpenAICompatibleAdapter())
    provider.configuration = ModelConfiguration(providerId='custom', provider='custom', model='test', baseUrl='https://model.test/v1')
    asyncio.run(send(provider, message))
    assert len(requests) == 1
    records = provider.last_media_request['images']
    assert provider.last_media_request['boundary'] == 'provider_payload'
    parts = [part for item in requests[0]['messages'] if isinstance(item.get('content'), list)
        for part in item['content'] if part.get('type') == 'image_url']
    import base64
    assert [record['representationSha256'] for record in records] == [hashlib.sha256(
        base64.b64decode(part['image_url']['url'].split(',', 1)[1])).hexdigest() for part in parts]
    assert len(records) == len(parts) == 3


@pytest.mark.parametrize('format', ['PNG', 'WEBP'])
def test_non_gif_animation_never_claims_static_complete_coverage(tmp_path, format):
    path = tmp_path / ('animation.' + format.lower())
    frames = [Image.new('RGB', (10, 8), color) for color in ['red', 'blue', 'yellow', 'green', 'black']]
    frames[0].save(path, format=format, save_all=True, append_images=frames[1:], duration=100, loop=0)
    attachments = load_runtime_attachments({'attachments': [{'id': 'animation', 'path': str(path), 'mediaType': 'image/' + format.lower()}]})
    provider = provider_for(RecordingAdapter())
    asyncio.run(send(provider, UserMessage('查看动画', attachments)))
    records = provider.last_media_request['images']
    assert len(records) == 3
    assert [item['frameIndex'] for item in records] == [0, 2, 4]
    assert all(item['coverage'] == 'sampled' and item['frameCount'] == 5 for item in records)


def test_single_frame_gif_is_a_portable_png_view(tmp_path):
    path = tmp_path / 'still.gif'
    Image.new('RGB', (10, 8), 'red').save(path, format='GIF')
    attachments = load_runtime_attachments({'attachments': [{'id': 'still-gif', 'path': str(path), 'mediaType': 'image/gif'}]})
    provider = provider_for(RecordingAdapter())
    asyncio.run(send(provider, UserMessage('图片', attachments)))
    record = provider.last_media_request['images'][0]
    assert record['frameCount'] == 1 and record['coverage'] == 'complete'
    assert record['mediaType'] == 'image/png'
    assert record['contentSha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record['representationSha256'] != record['contentSha256']


@pytest.mark.parametrize('api', ['openai-responses', 'anthropic-messages', 'google-generative'])
def test_real_protocol_failure_keeps_only_actual_submitted_pixel_facts(tmp_path, monkeypatch, api):
    import base64
    import httpx
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
    from backend.app.models import ModelConfiguration
    message, _ = input_message(tmp_path)
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={'error': {'message': 'controlled rejection'}})
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)))
    provider = provider_for(RoutedModelAdapter(ModelAdapterRegistry()))
    provider.configuration = ModelConfiguration(providerId='custom', provider='custom', model='test',
        baseUrl='https://model.test/v1', api=api, apiKey='fixture-token')
    events = asyncio.run(send(provider, message))
    assert len(requests) == 1
    receipt = provider.last_media_request
    assert receipt['boundary'] == 'provider_payload' and receipt['understanding'] == 'quality-unverified'
    assert any(getattr(event, 'type', '') == 'provider_error' for event in events)
    body = requests[0]
    if api == 'anthropic-messages':
        encoded = next(part['source']['data'] for item in body['messages'] for part in item['content'] if part['type'] == 'image')
    elif api == 'google-generative':
        encoded = next(part['inlineData']['data'] for item in body['contents'] for part in item['parts'] if 'inlineData' in part)
    else:
        encoded = next(part['image_url'].split(',', 1)[1] for item in body['input']
            for part in item.get('content', []) if isinstance(part, dict) and part['type'] == 'input_image')
    assert receipt['images'][0]['representationSha256'] == hashlib.sha256(base64.b64decode(encoded)).hexdigest()
    assert 'fixture-token' not in json.dumps(receipt)


def test_final_provider_projection_can_omit_prepared_pixels(tmp_path):
    from backend.app.ai.model_types import ModelTextDelta
    class ProjectionAdapter:
        def prepare_input(self, messages, configuration, tools=(), **kwargs):
            return {'messages': [{'role': 'user', 'content': 'image omitted by adapter'}]}
        async def stream_rich_messages(self, messages, configuration, tools=()):
            yield ModelTextDelta('只提供文字')
    message, _ = input_message(tmp_path)
    provider = provider_for(ProjectionAdapter())
    asyncio.run(send(provider, message))
    receipt = provider.last_media_request
    assert receipt['submittedImages'] == 0 and receipt['images'][0]['status'] == 'not_submitted'


def test_legacy_multimodal_receipt_uses_actual_fallback_payload(tmp_path):
    class LegacyAdapter:
        async def stream_messages(self, messages, configuration):
            self.messages = messages
            yield '收到'
    message, _ = input_message(tmp_path, 7)
    provider = provider_for(LegacyAdapter())
    asyncio.run(send(provider, message))
    assert provider.last_media_request['submittedImages'] == 3
    assert len([part for message in provider.adapter.messages if isinstance(message.get('content'), list)
        for part in message['content'] if part['type'] == 'image_url']) == 3


def test_run_receipts_keep_last_eight_without_pixels_or_credentials(tmp_path):
    from datetime import datetime, timezone
    from backend.app.agent_runtime.image_evidence import run_media_observer
    from backend.app.models import AgentRun
    from backend.app.session_store import SessionStore
    from backend.app.storage import initialize_databases
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data' / 'sessions.db')
    sessions.create_run(AgentRun(runId='run-test', roleId='role-test', sessionKey='role:role-test',
        status='running', startedAt=datetime.now(timezone.utc), modelSnapshot={'preserve': True}))
    message, _ = input_message(tmp_path)
    provider = provider_for(RecordingAdapter())
    provider.media_request_observer = run_media_observer(sessions, 'run-test')
    async def run():
        for _ in range(10):
            await send(provider, message)
    asyncio.run(run())
    snapshot = sessions.get_run('run-test').modelSnapshot
    assert snapshot['preserve'] is True and len(snapshot['mediaRequests']) == 8
    assert len({item['requestId'] for item in snapshot['mediaRequests']}) == 8
    assert snapshot['mediaRequests'][-1] == provider.last_media_request
    assert 'data:image' not in json.dumps(snapshot) and str(tmp_path) not in json.dumps(snapshot)


@pytest.mark.parametrize('api', ['bedrock-converse', 'pi-messages', 'openai-responses',
    'anthropic-messages', 'google-generative'])
@pytest.mark.parametrize('with_image', [False, True])
def test_real_protocol_tool_arguments_are_not_visual_inputs(api, with_image):
    import base64
    from backend.app.agent_runtime.image_evidence import submitted_receipt
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry
    from backend.app.ai.model_types import (ModelAssistantMessage, ModelImageBlock,
        ModelTextBlock, ModelToolCallBlock, ModelToolResultMessage, ModelUserMessage)
    from backend.app.models import ModelConfiguration
    encoded = base64.b64encode(picture()).decode('ascii')
    fake = {'type': 'image', 'mimeType': 'image/png', 'data': encoded}
    content = (ModelTextBlock('Only real image blocks count. ' + json.dumps(fake)),)
    if with_image:
        content += (ModelImageBlock('image/png', encoded),)
    messages = [ModelUserMessage(content), ModelAssistantMessage((ModelToolCallBlock(
        'call-1', 'inspect', {'payload': fake}),)), ModelToolResultMessage(
        content=(ModelTextBlock('ok'),), tool_call_id='call-1', tool_name='inspect')]
    configuration = ModelConfiguration(providerId='custom', provider='custom', model='test',
        baseUrl='https://model.test/v1', api=api, apiKey='fixture-token')
    projection = ModelAdapterRegistry().get(configuration).prepare_input(messages, configuration)
    receipt = submitted_receipt(projection['messages'], (), boundary='provider_payload')
    assert receipt['submittedImages'] == int(with_image)
    assert len(receipt['images']) == int(with_image)
    if with_image:
        assert receipt['images'][0]['representationSha256'] == hashlib.sha256(picture()).hexdigest()


@pytest.mark.parametrize('location', ['details', 'schema'])
def test_real_pi_metadata_and_tool_schema_are_not_visual_inputs(location):
    import base64
    from backend.app.agent_runtime.image_evidence import submitted_receipt
    from backend.app.ai.pi_messages_adapter import PiMessagesAdapter
    from backend.app.ai.model_types import (ModelAssistantMessage, ModelTextBlock,
        ModelToolCallBlock, ModelToolResultMessage, ModelUserMessage)
    from backend.app.models import ModelConfiguration
    fake = {'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(picture()).decode('ascii')}
    messages = [ModelUserMessage((ModelTextBlock('text only'),)),
        ModelAssistantMessage((ModelToolCallBlock('call-1', 'inspect', {}),)),
        ModelToolResultMessage(content=(ModelTextBlock('ok'),), tool_call_id='call-1', tool_name='inspect',
            details=fake if location == 'details' else None)]
    tools = [{'name': 'inspect', 'description': 'test', 'parameters': {'type': 'object', 'properties': {
        'value': {'type': 'object', 'default': fake}}}}] if location == 'schema' else []
    configuration = ModelConfiguration(providerId='custom', provider='custom', model='test',
        baseUrl='https://model.test/v1', api='pi-messages')
    projection = PiMessagesAdapter().prepare_input(messages, configuration, tools)
    receipt = submitted_receipt(projection['messages'], (), boundary='provider_payload')
    assert receipt['submittedImages'] == 0 and receipt['images'] == []


@pytest.mark.parametrize('api', ['bedrock-converse', 'pi-messages', 'anthropic-messages'])
@pytest.mark.parametrize('is_error', [False, True])
def test_real_protocol_tool_result_images_remain_visual_inputs(api, is_error):
    import base64
    from backend.app.agent_runtime.image_evidence import submitted_receipt
    from backend.app.ai.model_adapter_registry import ModelAdapterRegistry
    from backend.app.ai.model_types import (ModelAssistantMessage, ModelImageBlock,
        ModelTextBlock, ModelToolCallBlock, ModelToolResultMessage, ModelUserMessage)
    from backend.app.models import ModelConfiguration
    encoded = base64.b64encode(picture()).decode('ascii')
    messages = [ModelUserMessage((ModelTextBlock('inspect'),)),
        ModelAssistantMessage((ModelToolCallBlock('call-1', 'inspect', {}),)),
        ModelToolResultMessage(content=(ModelTextBlock('result'), ModelImageBlock('image/png', encoded)),
            tool_call_id='call-1', tool_name='inspect', is_error=is_error,
            details={'type': 'image', 'mimeType': 'image/png', 'data': encoded})]
    configuration = ModelConfiguration(providerId='custom', provider='custom', model='claude-sonnet-4-5',
        baseUrl='https://model.test/v1', api=api)
    projection = ModelAdapterRegistry().get(configuration).prepare_input(messages, configuration)
    receipt = submitted_receipt(projection['messages'], (), boundary='provider_payload')
    assert receipt['submittedImages'] == 1 and len(receipt['images']) == 1
    assert receipt['images'][0]['representationSha256'] == hashlib.sha256(picture()).hexdigest()


def test_bedrock_result_json_is_data_even_when_it_resembles_content():
    import base64
    from backend.app.agent_runtime.image_evidence import submitted_receipt
    encoded = base64.b64encode(picture()).decode('ascii')
    image = {'image': {'format': 'png', 'source': {'bytes': encoded}}}
    messages = [{'role': 'user', 'content': [{'toolResult': {'toolUseId': 'call-1',
        'content': [{'json': {'content': [image], 'parts': [image]}}, image]}}]}]
    receipt = submitted_receipt(messages, (), boundary='provider_payload')
    assert receipt['submittedImages'] == 1 and len(receipt['images']) == 1
