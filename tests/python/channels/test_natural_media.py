"""Natural media uses actual bytes, isolated library/clock and no live I/O."""
import asyncio
import base64
import io
import json
from dataclasses import replace
from types import SimpleNamespace

from PIL import Image
import pytest

from backend.app.agent_runtime.provider import CancellationToken
from backend.app.channels.models import Attachment, ChatType, InboundEvent
from backend.app.channels.natural_media import NaturalMedia, MEME_COOLDOWN
from test_chat_meme_library import real_library, picture


def setup(tmp_path, *, format='PNG', vision=True, authorized=lambda: True, spend=lambda n: True):
    library = real_library(tmp_path / 'library')
    root = library.library.root('alice')
    (root / 'happy').mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps({'version': 1, 'categories': {
        'happy': {'enabled': True, 'desc': 'happy', 'aliases': ['yay']}}}))
    extension = {'PNG':'png', 'JPEG':'jpg', 'WEBP':'webp', 'GIF':'gif'}[format]
    raw = picture(format)
    (root / 'happy' / ('image.'+extension)).write_bytes(raw)
    now = [0.0]
    downloads = []
    async def download(url, *, max_bytes):
        downloads.append(url)
        return raw, 'ignored/untrusted'
    service = NaturalMedia(library, tmp_path / 'cache', clock=lambda: now[0], downloader=download)
    event = InboundEvent('qq', 'account', 'event', 'message', 'group', ChatType.GROUP,
        'sender', content='happy', triggered=False, attachments=(Attachment('image', 'https://qq.test/current'),))
    item = SimpleNamespace(message_id=event.external_message_id, content=event.content, event=event)
    context = SimpleNamespace(role_id='alice', session_key='thread:group', run_id='natural-run',
        tool_call_id='natural-media', signal=CancellationToken())
    params = dict(role_id='alice', account_id='account', thread_id='persisted-thread-uuid', chat_id='group', batch_id='batch',
        inputs=[item], deadline=60.0, authorized=authorized, context=context, vision_enabled=vision, spend=spend)
    return SimpleNamespace(service=service, library=library, root=root, now=now, raw=raw,
        downloads=downloads, item=item, context=context, params=params)


@pytest.mark.parametrize('format', ['PNG','JPEG','WEBP','GIF'])
def test_real_vision_and_library_original_preserved(tmp_path, format):
    async def run():
        state = setup(tmp_path, format=format)
        batch = await state.service.prepare(**state.params)
        assert 1 <= len(batch.attachments) <= 3
        for attachment in batch.attachments:
            decoded = base64.b64decode(attachment.image.data_base64)
            image = Image.open(io.BytesIO(decoded))
            assert image.format == 'PNG' and image.size == (4, 3)
            assert max(image.getpixel((0, 0))) > 240
        if format == 'GIF':
            assert len(batch.attachments) == 2
        assert next(batch.path.glob('*.original')).read_bytes() == state.raw
        assert not (state.library.attachment_root('alice') / 'attachments').exists()
        identifier = batch.candidates[0]['id']
        metadata, raw = await batch.resolve(identifier)
        assert raw == state.raw
        if format == 'GIF':
            assert Image.open(io.BytesIO(raw)).n_frames == 2
        assert metadata['kind'] == 'image'
        batch.close()
        assert not list(state.service.root.iterdir())
        assert state.service.cache_bytes == 0
        assert not batch.valid(identifier)
    asyncio.run(run())


def test_no_vision_no_download_still_candidates_and_image_fact(tmp_path):
    async def run():
        state = setup(tmp_path, vision=False)
        batch = await state.service.prepare(**state.params)
        assert batch.image_present and not batch.attachments and batch.candidates
        assert not state.downloads
        batch.close()
    asyncio.run(run())


def test_bad_bytes_and_download_failure_are_not_image_descriptions(tmp_path):
    async def run():
        state = setup(tmp_path)
        async def bad(*args, **kwargs):
            return b'not an image', 'image/png'
        state.service.downloader = bad
        batch = await state.service.prepare(**state.params)
        assert batch.image_present and not batch.attachments
        assert '不得猜测' in batch.prompt
        batch.close()
        async def failed(*args, **kwargs):
            raise OSError('offline')
        state.service.downloader = failed
        batch = await state.service.prepare(**state.params)
        assert not batch.attachments
        batch.close()
    asyncio.run(run())


def test_quote_must_be_verified_current_group_and_sources_finite(tmp_path):
    async def run():
        state = setup(tmp_path)
        event = replace(state.item.event, reply_to_message_id='quote', attachments=(), metadata={
            'quotedAttachments': (Attachment('image','https://qq.test/quote'),), 'quotedGroupVerified': False})
        state.item.event = event
        batch = await state.service.prepare(**state.params)
        assert not state.downloads
        batch.close()
        state.item.event = replace(event, metadata={**event.metadata,'quotedGroupVerified':True})
        batch = await state.service.prepare(**state.params)
        assert state.downloads == ['https://qq.test/quote']
        batch.close()
        state.item.event = replace(event, chat_id='other', attachments=(Attachment('image','https://qq.test/other'),))
        batch = await state.service.prepare(**state.params)
        assert state.downloads == ['https://qq.test/quote']
        batch.close()
    asyncio.run(run())


def test_candidate_forgery_replacement_disable_and_new_batch(tmp_path):
    async def run():
        state = setup(tmp_path)
        batch = await state.service.prepare(**state.params)
        identifier = batch.candidates[0]['id']
        for forged in ['https://qq.test/image', '/tmp/image.png', 'attachment-id', None, {}]:
            assert not batch.valid(forged)
            with pytest.raises(PermissionError):
                await batch.resolve(forged)
        next_batch = await state.service.prepare(**state.params)
        assert not batch.valid(identifier) and not next_batch.valid(identifier)
        identifier = next_batch.candidates[0]['id']
        (state.root / 'happy' / 'image.png').write_bytes(picture('PNG','blue'))
        assert not next_batch.valid(identifier)
        next_batch.close()
        batch = await state.service.prepare(**state.params)
        identifier = batch.candidates[0]['id']
        (state.root / 'manifest.json').write_text(json.dumps({'version':1,'categories':{
            'happy': {'enabled': False, 'desc': 'happy'}}}))
        assert not batch.valid(identifier)
        batch.close()
        batch = await state.service.prepare(**state.params)
        assert not batch.candidates
        batch.close()
    asyncio.run(run())


@pytest.mark.parametrize('status', ['sent', 'failed', 'uncertain'])
def test_attempt_success_and_cooldown_are_distinct(tmp_path, status):
    async def run():
        state = setup(tmp_path)
        batch = await state.service.prepare(**state.params)
        identifier = batch.candidates[0]['id']
        await batch.resolve(identifier)
        batch.attempt(identifier)
        batch.result(identifier, status)
        batch.result(identifier, status)
        counts = next(iter(state.service.usage.values()))
        assert counts['attempts'] == 1 and counts['successes'] == int(status == 'sent')
        with pytest.raises(PermissionError):
            await batch.resolve(identifier)
        batch.close()
        batch = await state.service.prepare(**state.params)
        assert not batch.candidates
        batch.close()
        state.now[0] = MEME_COOLDOWN + 1
        batch = await state.service.prepare(**{**state.params, 'deadline':120})
        assert batch.candidates
        batch.close()
    asyncio.run(run())


def test_ttl_cancel_restart_cleanup_never_touches_formal_library(tmp_path):
    async def run():
        state = setup(tmp_path)
        batch = await state.service.prepare(**state.params)
        assert batch.path.exists() and state.service.cache_bytes
        state.now[0] = 61
        state.service.prune()
        assert batch.closed and not batch.path.exists()
        state.now[0] = 0
        batch = await state.service.prepare(**state.params)
        state.context.signal.cancel()
        state.service.prune()
        assert batch.closed
        leftover = state.service.root / 'abrupt-exit'
        leftover.mkdir()
        (leftover/'raw').write_bytes(state.raw)
        NaturalMedia(state.library, tmp_path/'cache')
        assert not leftover.exists()
        assert (state.root/'happy'/'image.png').read_bytes() == state.raw
    asyncio.run(run())


def test_revocation_during_download_and_budget_exhaustion(tmp_path):
    async def run():
        allowed = [True]
        charged = []
        state = setup(tmp_path, authorized=lambda: allowed[0], spend=lambda n: charged.append(n) is None)
        async def revoke(*args, **kwargs):
            allowed[0] = False
            return state.raw, 'image/png'
        state.service.downloader = revoke
        with pytest.raises(PermissionError):
            await state.service.prepare(**state.params)
        assert not list(state.service.root.iterdir()) and len(charged) == 2
        allowed[0] = True
        with pytest.raises(ValueError):
            await state.service.prepare(**{**state.params, 'spend':lambda n:False})
        assert not state.service.batches
    asyncio.run(run())


def test_capacity_and_input_count_are_bounded(tmp_path, monkeypatch):
    from backend.app.channels import natural_media
    async def run():
        state = setup(tmp_path)
        state.item.event = replace(state.item.event, attachments=tuple(
            Attachment('image', 'https://qq.test/'+str(index)) for index in range(20)))
        batch = await state.service.prepare(**state.params)
        assert len(state.downloads) == natural_media.MAX_INPUT_IMAGES
        assert len(batch.attachments) == natural_media.MAX_INPUT_IMAGES
        batch.close()
        monkeypatch.setattr(natural_media, 'MAX_CACHE_BYTES', len(state.raw)-1)
        batch = await state.service.prepare(**state.params)
        assert not batch.attachments and state.service.cache_bytes == 0
        batch.close()
    asyncio.run(run())


def test_cancel_during_download_cleans_originals(tmp_path):
    async def run():
        state = setup(tmp_path)
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        state.service.downloader = blocked
        task = asyncio.create_task(state.service.prepare(**state.params))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not state.service.batches and not list(state.service.root.iterdir())
    asyncio.run(run())


def test_actual_receipt_after_expiry_still_counts_success(tmp_path):
    async def run():
        state = setup(tmp_path)
        batch = await state.service.prepare(**state.params)
        identifier = batch.candidates[0]['id']
        batch.attempt(identifier)
        batch.close()  # external transport already owns the attempt
        batch.result(identifier, 'sent')
        assert next(iter(state.service.usage.values()))['successes'] == 1
    asyncio.run(run())


def test_empty_library_and_cross_role_context_are_rejected(tmp_path):
    async def run():
        state = setup(tmp_path)
        (state.root/'happy'/'image.png').unlink()
        batch = await state.service.prepare(**state.params)
        assert not batch.candidates
        batch.close()
        state.context.role_id = 'bob'
        with pytest.raises(PermissionError):
            await state.service.prepare(**state.params)
    asyncio.run(run())


def test_natural_reference_journal_has_no_owner_principal_and_recovers_factually(tmp_path, monkeypatch):
    from backend.app.channels.meme_library import _OwnerPublication
    from backend.app.proactive_runtime import meme_recovery
    async def run():
        state = setup(tmp_path)
        batch = await state.service.prepare(**state.params)
        identifier = batch.candidates[0]['id']
        original_close = _OwnerPublication.close
        original_publish = _OwnerPublication.publish
        def interrupted(self, path):
            original_publish(self, path)
            raise RuntimeError('simulated interruption after publication')
        monkeypatch.setattr(_OwnerPublication, 'publish', interrupted)
        monkeypatch.setattr(_OwnerPublication, 'close', lambda self: None)
        with pytest.raises(RuntimeError):
            await batch.resolve(identifier)
        with state.library.library.app._transaction() as db:
            row = db.execute('SELECT * FROM autonomy_meme_operations').fetchone()
        plan = json.loads(row['planned'])
        assert plan['principal'] == 'natural' and plan['kind'] == 'natural_chat_reference'
        stages = [item['stage'] for item in plan['files']]
        from pathlib import Path
        published = {item['target']:Path(item['target']).read_bytes()
                     for item in plan['files'] if Path(item['target']).exists()}
        monkeypatch.setattr(_OwnerPublication, 'close', original_close)
        monkeypatch.setattr(_OwnerPublication, 'publish', original_publish)
        meme_recovery.reconcile(state.library.library, 'alice')
        assert all(not Path(path).exists() for path in stages)
        assert all(Path(path).read_bytes() == data for path, data in published.items())
        with state.library.library.app._transaction() as db:
            row = db.execute('SELECT * FROM autonomy_meme_operations').fetchone()
        assert row['status'] == 'reconciled'
        batch.close()
    asyncio.run(run())


@pytest.mark.parametrize('error_type', ['ConnectError', 'ReadTimeout', 'HTTPStatusError'])
def test_real_downloader_http_errors_degrade_to_image_fact(tmp_path, error_type):
    import httpx
    async def run():
        state = setup(tmp_path)
        async def failed(*args, **kwargs):
            if error_type == 'HTTPStatusError':
                request = httpx.Request('GET', 'https://qq.test/image')
                raise httpx.HTTPStatusError('blocked',request=request,response=httpx.Response(403,request=request))
            raise getattr(httpx,error_type)('offline')
        state.service.downloader = failed
        batch = await state.service.prepare(**state.params)
        assert batch.image_present and not batch.attachments and batch.candidates
        batch.close()
    asyncio.run(run())


def test_repeated_cancel_waits_for_owned_worker_before_return():
    import threading
    from backend.app.channels.natural_media import _owned_worker
    async def run():
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def work():
            entered.set()
            release.wait(5)
            completed.set()
        task = asyncio.create_task(_owned_worker(work))
        while not entered.is_set(): await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not completed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert completed.is_set()
    asyncio.run(run())


def test_startup_purge_without_library_initialization_preserves_other_data(tmp_path):
    from backend.app.channels.natural_media import purge_cache
    root=tmp_path/'cache'
    leftover=root/'natural-media-v1'/'old-batch'
    leftover.mkdir(parents=True)
    (leftover/'original.gif').write_bytes(picture('GIF'))
    formal=tmp_path/'memes'
    formal.mkdir()
    (formal/'existing.gif').write_bytes(picture('GIF'))
    attachments=tmp_path/'attachments'
    attachments.mkdir()
    (attachments/'ordinary.png').write_bytes(picture())
    purge_cache(root)
    assert not leftover.exists() and not list((root/'natural-media-v1').iterdir())
    assert (formal/'existing.gif').read_bytes()==picture('GIF')
    assert (attachments/'ordinary.png').read_bytes()==picture()
