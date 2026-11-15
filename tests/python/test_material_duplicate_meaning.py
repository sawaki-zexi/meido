"""Identical pixels grant neither another source's meaning nor a lost edit."""
import asyncio
import json
from types import SimpleNamespace

from test_material_library_api import api, current, put
from test_chat_meme_library import upload, picture
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.channels.meme_tools import ChatMemeTools


def context(session):
    return SimpleNamespace(role_id='alice', session_key=session, run_id='run-' + session,
        tool_call_id='save', signal=CancellationToken())


def save(chat, attachment, session, description='', category='收藏'):
    ctx = context(session)
    service = ChatMemeTools(chat, None, role_id='alice', session_key=session, run_id=ctx.run_id,
        account_id='a', thread_id=session, target_id='t', attachments=[attachment], authorized=lambda: True,
        permitted=lambda name: True, owner_text='收藏这张，说明：' + description,
        source={'kind': 'chat', 'sessionKey': session})
    tool = next(tool for tool in service.tools() if tool.definition.name == 'chat_meme_save')
    result = asyncio.run(tool.execute({'description': description, 'category': category}, ctx))
    assert not result.is_error, result.content
    return result.details


def test_same_pixels_in_group_b_do_not_grant_group_a_meaning_or_search_text(api):
    client, service, chat = api
    attachment = upload(chat)
    save(chat, attachment, 'thread:a', 'A群私有暗号', 'A群秘密分类')
    manifest = chat.library.root('alice') / 'manifest.json'
    categories = json.loads(manifest.read_text(encoding='utf-8'))
    categories['categories']['A群秘密分类']['aliases'] = ['仅A可见的别名']
    manifest.write_text(json.dumps(categories), encoding='utf-8')
    result = save(chat, attachment, 'thread:b')
    assert 'A群' not in json.dumps(result, ensure_ascii=False)
    assert chat.candidates('alice', context=context('thread:b')) == ()
    item, = chat.candidates('alice', context=context('thread:b'), explicit=True)
    assert item.description == '' and item.semantic_status == 'pending'
    assert 'A群' not in json.dumps(item.public(), ensure_ascii=False)
    assert chat.candidates('alice', 'A群', context=context('thread:b'), explicit=True) == ()
    assert chat.candidates('alice', '仅A', context=context('thread:b'), explicit=True) == ()
    original, = chat.candidates('alice', context=context('thread:a'))
    assert original.description == 'A群私有暗号'
    save(chat, attachment, 'thread:b', 'B群自己的说明')
    own, = chat.candidates('alice', context=context('thread:b'))
    assert own.description == 'B群自己的说明'
    assert not chat.valid('alice', item)
    assert chat.candidates('alice', context=context('thread:a'))[0].description == original.description
    assert len(list(chat.library.root('alice').glob('*/*.png'))) == 1


def test_scope_local_fill_reports_conflicting_tool_proposal_and_replacement_clears_every_scope(api):
    client, service, chat = api
    attachment = upload(chat)
    save(chat, attachment, 'thread:a', 'A群私有暗号')
    save(chat, attachment, 'thread:b')
    filled = save(chat, upload(chat), 'thread:b', 'B群说明')
    assert filled['descriptionOutcome'] == 'filled'
    conflict = save(chat, upload(chat), 'thread:b', 'B群另一份建议')
    assert conflict['descriptionOutcome'] == 'proposal_retained'
    assert conflict['description'] == 'B群说明'
    assert 'A群私有暗号' not in json.dumps(conflict, ensure_ascii=False)
    a, = chat.candidates('alice', context=context('thread:a'))
    b, = chat.candidates('alice', context=context('thread:b'))
    a.path.write_bytes(picture(color='blue'))
    assert not chat.valid('alice', a) and not chat.valid('alice', b)
    for session, old in [('thread:a', 'A群私有暗号'), ('thread:b', 'B群说明')]:
        assert chat.candidates('alice', context=context(session)) == ()
        pending, = chat.candidates('alice', context=context(session), explicit=True)
        assert pending.description == '' and pending.semantic_status == 'content_changed'
    latest = current(client)['materials'][0]
    assert latest['history'][-1]['description'] == 'A群私有暗号'
    assert len(list(chat.library.root('alice').glob('*/*.png'))) == 1


def test_pre_fix_record_does_not_retroactively_share_first_source_meaning(api):
    client, service, chat = api
    attachment = upload(chat)
    save(chat, attachment, 'thread:a', '旧记录的私有说明')
    save(chat, attachment, 'thread:b')
    path = next((chat.library.root('alice').parent / '.meido-memes/chat').glob('*.json'))
    value = json.loads(path.read_text(encoding='utf-8'))
    value.pop('meaningSources')
    value.pop('scopedMeanings')
    path.write_text(json.dumps(value), encoding='utf-8')
    assert chat.candidates('alice', context=context('thread:b')) == ()
    candidate, = chat.candidates('alice', context=context('thread:b'), explicit=True)
    assert candidate.description == '' and candidate.category_label == '收藏'


def test_blank_owner_import_does_not_promote_private_meaning_but_can_fill_its_own(api):
    client, service, chat = api
    save(chat, upload(chat), 'thread:a', '群内专属说明', '秘密分类')
    imported = put(client, description='')
    assert imported.status_code == 200, imported.text
    assert chat.candidates('alice') == ()
    pending, = chat.candidates('alice', explicit=True)
    assert pending.description == ''
    assert chat.candidates('alice', '秘密', explicit=True) == ()
    filled = put(client, description='主人提供的共享说明')
    assert filled.status_code == 200, filled.text
    assert filled.json()['descriptionOutcome'] == 'filled'
    ready, = chat.candidates('alice')
    assert ready.description == '主人提供的共享说明'
    assert chat.candidates('alice', context=context('thread:a'))[0].description == '群内专属说明'


def test_duplicate_import_fills_pending_then_reports_conflict_without_losing_input(api):
    client, service, chat = api
    original = put(client, description='').json()['material']
    stale = current(client)['revision']
    filled = put(client, description='真正补充的说明')
    assert filled.status_code == 200, filled.text
    assert filled.json()['material']['description'] == '真正补充的说明'
    assert filled.json()['descriptionOutcome'] == 'filled'
    conflict = put(client, description='另一份不同说明')
    assert conflict.status_code == 200, conflict.text
    assert conflict.json()['descriptionOutcome'] == 'proposal_retained'
    assert conflict.json()['material']['description'] == '真正补充的说明'
    assert client.patch('/api/roles/alice/materials/' + original['id'], json={
        'expectedRevision': stale, 'description': '旧页面覆盖'}).status_code == 409
    record = json.loads(next((chat.library.root('alice').parent / '.meido-memes/chat').glob('*.json')).read_text(encoding='utf-8'))
    assert '另一份不同说明' in json.dumps(record, ensure_ascii=False)
    assert record['history'][-1]['description'] == ''
    assert len(list(chat.library.root('alice').glob('*/*.png'))) == 1


def test_concurrent_pending_fills_use_the_observed_revision_and_preserve_original(api):
    from concurrent.futures import ThreadPoolExecutor
    client, service, chat = api
    item = put(client, description='').json()['material']
    revision = current(client)['revision']
    with ThreadPoolExecutor(max_workers=2) as workers:
        responses = list(workers.map(lambda description: put(client, revision=revision, description=description),
            ['第一份说明', '第二份说明']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert winner['descriptionOutcome'] == 'filled'
    current_item, = current(client)['materials']
    assert current_item['description'] == winner['material']['description']
    assert current_item['assetId'] == item['assetId']
    assert client.get(current_item['imageUrl']).content == picture()
