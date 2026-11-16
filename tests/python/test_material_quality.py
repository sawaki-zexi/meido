"""Joint actual read-receipt and selection corpus; no real model quality claim."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from test_chat_meme_library import real_library, picture
from test_current_image_evidence import RecordingAdapter, provider_for, send
from backend.app.agent_runtime.types import UserMessage
from backend.app.session_store import load_runtime_attachments
from backend.app.channels import meme_semantics as semantics

FIXTURES = Path(__file__).parents[1] / 'fixtures'
CORPUS = json.loads((FIXTURES / 'material-quality.json').read_text(encoding='utf-8'))


def sample_bytes(sample, ordinal):
    if sample['kind'] == 'photo' or sample.get('failure') == 'unsupported':
        return (FIXTURES / 'material-quality/apollo17-earth.jpg').read_bytes(), 'jpg', 'image/jpeg'
    if sample.get('failure') == 'corrupt':
        return b'GIF89a broken animation', 'gif', 'image/gif'
    frames = []
    for index in range(sample.get('frames', 1)):
        frame = Image.new('RGB', (240, 100), (230, 240 - ordinal * 4, 190 + index * 5))
        ImageDraw.Draw(frame).text((8, 16), sample.get('text', sample['id']) + f' {ordinal}:{index}', fill='black')
        frames.append(frame)
    stream = io.BytesIO()
    fmt = 'GIF' if len(frames) > 1 else 'PNG'
    frames[0].save(stream, format=fmt, **({'save_all': True, 'append_images': frames[1:], 'duration': 100} if len(frames) > 1 else {}))
    return stream.getvalue(), fmt.lower(), 'image/' + fmt.lower()


def test_read_evidence_and_selection_fixed_corpus(tmp_path):
    assert len(CORPUS['samples']) >= 20 and CORPUS['qualityStatus'] == 'quality-unverified'
    report = {'qualityStatus': 'quality-unverified', 'modelExecuted': False, 'samples': []}
    for ordinal, sample in enumerate(CORPUS['samples']):
        directory = tmp_path / sample['id']
        directory.mkdir()
        raw, suffix, mime = sample_bytes(sample, ordinal)
        path = directory / ('input.' + suffix)
        path.write_bytes(raw)
        message = UserMessage('描述当前提供的图片，不足时说明不确定。', load_runtime_attachments({
            'attachments': [{'id': sample['id'], 'path': str(path), 'mediaType': mime}]}),
            source_message_ids=(sample['id'],))
        provider = provider_for(RecordingAdapter(sample.get('failure') != 'unsupported'))
        asyncio.run(send(provider, message))
        receipt = provider.last_media_request
        images = receipt['images']
        if not sample.get('failure'):
            assert images and all(item['status'] == 'pixels_submitted' for item in images)
            count = sample.get('frames', 1)
            assert [item['frameIndex'] for item in images] == sorted({0, count // 2, count - 1})
            assert all(item['contentSha256'] == hashlib.sha256(raw).hexdigest() for item in images)
        elif sample.get('failure') == 'corrupt':
            assert not images
            assert '图片读取失败' in str(provider.adapter.requests)
            images = [{'status': 'read_failed', 'coverage': 'none'}]
        else:
            assert images and all(item['status'] != 'pixels_submitted' for item in images)
        chat = real_library(directory / 'library')
        root = chat.library.root('alice')
        root.mkdir(parents=True, exist_ok=True)
        categories = {'forbidden': {'desc': '禁止选择的侮辱表情', 'enabled': True}}
        forbidden = root / 'forbidden/original.png'
        forbidden.parent.mkdir(); forbidden.write_bytes(picture())
        if sample.get('failure') != 'corrupt':
            original = root / sample['id'] / path.name
            original.parent.mkdir(); original.write_bytes(raw)
            categories[sample['id']] = {'desc': '', 'enabled': True}
            digest = hashlib.sha256(raw).hexdigest()
            value = semantics.new_meaning('alice', digest, original.relative_to(root).as_posix(), sample['description'],
                basis={'kind': 'owner_confirmed'}, source={'kind': 'owner_import', 'scope': 'role'}, category=sample['id'])
            value.update(uncertainty=sample.get('uncertainty', ''), uncertaintyAffectsUse=sample.get('uncertaintyAffectsUse', False))
            if sample['kind'] == 'group':
                source = {'kind': 'chat', 'sessionKey': 'group-a'}
                value['sources'].append(source)
                value = semantics.revise_management(value, {'meaningScopeId': semantics.scope_id(source),
                    'description': sample['groupDescription']}, expected_revision=value['revision'],
                    basis={'kind': 'owner_confirmed'}, author={'kind': 'owner'})
            metadata = semantics.metadata_path(chat.library, 'alice', digest)
            metadata.parent.mkdir(parents=True, exist_ok=True); metadata.write_bytes(semantics.encode(value))
        (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': categories}), encoding='utf-8')
        candidates = chat.candidates('alice', sample['query'], SimpleNamespace(session_key=sample.get('session', '')))
        actual = [item.category for item in candidates]
        assert set(actual) == set(sample['allowed']), (sample['id'], actual)
        assert not set(actual).intersection(sample['forbidden'])
        report['samples'].append({'id': sample['id'], 'kind': sample['kind'],
            'readEvidence': [{key: image.get(key) for key in ('status', 'frameIndex', 'frameCount', 'coverage')} for image in images],
            'acceptableDescription': sample['acceptableDescription'], 'descriptionBasis': 'none' if sample.get('failure') == 'corrupt' else 'owner_confirmed',
            'modelDescription': None, 'query': sample['query'], 'selected': actual,
            'forbidden': sample['forbidden'], 'forbiddenSelected': [], 'result': 'pass'})
    output = Path('.data/t4-quality-report.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
