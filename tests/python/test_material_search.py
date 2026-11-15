"""Real originals and public queries; index work is measured separately from dispatch."""
import json
import time
import pytest
from pathlib import Path
from types import SimpleNamespace

from test_chat_meme_library import real_library, picture
from backend.app.channels.meme_library import ChatMemeLibrary
from backend.app.material_library import OwnerMaterialLibrary
from test_material_library_api import api, put, current
from backend.app.channels.meme_index import shared_index
from backend.app.channels import meme_semantics


def seed(chat, count=8):
    root = chat.library.root('alice')
    (root / 'coding').mkdir(parents=True, exist_ok=True)
    (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': {
        'coding': {'desc': '猫捂脸，表示编译失败，写不出代码', 'aliases': ['卡住', 'BUILD'], 'enabled': True}}}), encoding='utf-8')
    for index in range(count):
        (root / 'coding' / f'{index:03}.png').write_bytes(picture(color=(index % 256, index // 256, 15)))
    return root


def test_chinese_context_alias_ascii_and_paging_share_management_semantics(tmp_path):
    chat = real_library(tmp_path)
    seed(chat)
    owner = OwnerMaterialLibrary(chat, lambda role: object())
    for query in ('今天编译又失败了', '总是卡住怎么办', 'build', '这段代码写不出来'):
        items = chat.candidates('alice', query)
        assert len(items) == 4
        assert {item.asset_id for item in items} <= {item['assetId'] for item in owner.list('alice', query)['materials']}
        assert all(item.public()['reason'] for item in items)
    assert chat.candidates('alice', '天气晴朗适合散步') == ()
    first = chat.candidates('alice', '编译', explicit=True, limit=3)
    second = chat.candidates('alice', '编译', explicit=True, limit=3, offset=3)
    assert len(second) == 3 and not {item.asset_id for item in first} & {item.asset_id for item in second}


def test_warm_queries_across_facades_never_read_or_decode_unchanged_originals(tmp_path, monkeypatch):
    import backend.app.channels.meme_library as module
    chat = real_library(tmp_path)
    root = seed(chat, 50)
    chat.candidates('alice', '编译')
    reads, decodes = [], []
    original_read, original_image = module._read, module._image
    def read(path, limit):
        if path.suffix == '.png':
            reads.append(path)
        return original_read(path, limit)
    def decode(raw):
        decodes.append(len(raw))
        return original_image(raw)
    monkeypatch.setattr(module, '_read', read)
    monkeypatch.setattr(module, '_image', decode)
    other = ChatMemeLibrary(chat.library, chat.attachment_root)
    assert other.candidates('alice', '编译')
    assert not reads and not decodes
    (root / 'coding' / '000.png').write_bytes(picture(color='purple'))
    assert other.candidates('alice', '编译')
    assert len(reads) == len(decodes) == 1


def test_http_search_pages_and_cache_recovery_keep_owner_meaning(api, monkeypatch):
    client, owner, chat = api
    saved = put(client, description='编译错误时用猫捂脸').json()['material']
    candidate, = chat.candidates('alice', '今天编译又出错了')
    index = shared_index(chat.library)
    index.images.clear()
    for key in index.records:
        index.records[key] = 'corrupt derived entry'
    assert chat.candidates('alice', '编译')[0].asset_id == saved['assetId']
    updated = client.patch('/api/roles/alice/materials/' + saved['id'], json={
        'expectedRevision': current(client)['revision'], 'description': '开心庆祝完成任务'})
    assert updated.status_code == 200
    assert not chat.valid('alice', candidate)
    assert chat.candidates('alice', '编译错误') == ()
    assert chat.candidates('alice', '今天完成任务很开心')
    page = client.get('/api/roles/alice/materials', params={'query': '完成任务很开心', 'limit': 1, 'offset': 0}).json()
    assert page['total'] == 1 and page['nextOffset'] is None
    assert page['materials'][0]['history'] and page['materials'][0]['sources']
    # A failed cache refresh cannot revive the old successful meaning.
    index.records.clear()
    original = index.record
    monkeypatch.setattr(index, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('index refresh failed')))
    with pytest.raises(OSError):
        chat.candidates('alice', '开心')
    monkeypatch.setattr(index, 'record', original)
    assert chat.candidates('alice', '开心')[0].asset_id == saved['assetId']
    assert client.patch('/api/roles/alice/materials/' + saved['id'], json={
        'expectedRevision': current(client)['revision'], 'enabled': False}).status_code == 200
    assert chat.candidates('alice', '开心') == ()


@pytest.mark.parametrize('count', [50, 500])
def test_real_gallery_first_warm_and_single_change_work(tmp_path, monkeypatch, count):
    import backend.app.channels.meme_library as module
    chat = real_library(tmp_path)
    root = seed(chat, count)
    # Fixed mixed PNG/GIF corpus; individual originals have distinct pixels.
    for index in range(0, count, 10):
        (root / 'coding' / f'{index:03}.png').unlink()
        (root / 'coding' / f'{index:03}.gif').write_bytes(picture('GIF', (index % 256, index // 256, 15)))
    counts = {'bytes': 0, 'reads': 0, 'decodes': 0}
    original_read, original_image = module._read, module._image
    def read(path, limit):
        raw = original_read(path, limit)
        if path.suffix in {'.png', '.gif'}:
            counts['bytes'] += len(raw)
            counts['reads'] += 1
        return raw
    def decode(raw):
        counts['decodes'] += 1
        return original_image(raw)
    monkeypatch.setattr(module, '_read', read)
    monkeypatch.setattr(module, '_image', decode)
    report = {'items': count}
    for phase in ('first', 'warm', 'changed'):
        if phase == 'changed':
            (root / 'coding' / '001.png').write_bytes(picture(color='purple'))
        counts.update(bytes=0, reads=0, decodes=0)
        facade = ChatMemeLibrary(chat.library, chat.attachment_root)
        start = time.perf_counter()
        selected = facade.candidates('alice', '今天编译失败了')
        report[phase] = {**counts, 'milliseconds': round((time.perf_counter() - start) * 1000, 2),
            'candidateBytes': len(json.dumps([item.public() for item in selected], ensure_ascii=False).encode())}
        assert len(selected) == 4
        assert counts['reads'] == counts['decodes'] == {'first': count, 'warm': 0, 'changed': 1}[phase]
    print('MATERIAL_SEARCH_MEASUREMENT ' + json.dumps(report))


def test_fixed_quality_corpus_retrieval_rejects_all_forbidden_candidates(tmp_path):
    corpus = json.loads((Path(__file__).parents[1] / 'fixtures/material-search-quality.json').read_text(encoding='utf-8'))
    chat = real_library(tmp_path)
    root = chat.library.root('alice')
    categories = {}
    for ordinal, asset in enumerate(corpus['assets']):
        (root / asset['id']).mkdir(parents=True)
        fmt = asset['format']
        path = root / asset['id'] / ('original.' + {'JPEG': 'jpg'}.get(fmt, fmt.lower()))
        raw = picture(fmt, (ordinal * 20, 50, 100))
        path.write_bytes(raw)
        categories[asset['id']] = {'desc': asset['description'], 'aliases': asset.get('aliases', []), 'enabled': True}
        if asset.get('pending') or asset.get('session'):
            import hashlib
            digest = hashlib.sha256(raw).hexdigest()
            source = {'kind': 'chat', 'sessionKey': asset['session']} if asset.get('session') else {'kind': 'owner_import', 'scope': 'role'}
            record = meme_semantics.new_meaning('alice', digest, path.relative_to(root).as_posix(), asset['description'],
                basis={'kind': 'unverified' if asset.get('pending') else 'owner_confirmed'}, source=source, category=asset['id'])
            metadata = meme_semantics.metadata_path(chat.library, 'alice', digest)
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_bytes(meme_semantics.encode(record))
    (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': categories}), encoding='utf-8')
    for case in corpus['cases']:
        items = chat.candidates('alice', case['query'], SimpleNamespace(session_key=case.get('session', '')))
        actual = {item.category for item in items}
        assert actual == set(case['allowed']), (case['id'], actual)
    assert corpus['qualityStatus'] == 'quality-unverified'
