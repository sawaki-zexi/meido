"""Real natural intake, provider image payload, library and single QQ dispatch."""
import asyncio
import base64
import io
import json
from pathlib import Path

from PIL import Image
import pytest

from test_natural_turn import natural
from test_meme_turn import meme_turn, payload, image_bytes
from backend.app import main
from backend.app.models import ModelConfigurationInput
from backend.app.model_adapter import ModelTextDelta
from backend.app.channels import natural_media
_REAL_OWNED_WORKER = natural_media._owned_worker


@pytest.fixture
def media_turn(natural, monkeypatch):
    s = natural
    monkeypatch.setattr(natural_media,'_owned_worker',_REAL_OWNED_WORKER)
    s.raw = image_bytes('GIF')
    s.extension, s.media_type = 'gif', 'image/gif'
    (s.root/'happy'/'original.gif').write_bytes(s.raw)
    main.model_configuration_store.save(ModelConfigurationInput(providerId='custom',provider='test',
        baseUrl='https://model.test/v1',model='controlled-vision'))
    s.text, s.choose, s.before_result = '开心', True, lambda:None
    class Model:
        async def stream_structured_messages(self, messages, configuration, tools):
            assert not tools
            s.calls.append((messages,tools))
            system = messages[0]['content']
            candidates = json.JSONDecoder().raw_decode(system.split('候选：',1)[1])[0]
            s.before_result()
            result = {'action':'reply','text':s.text,'candidate_id':candidates[0]['id'] if candidates and s.choose else None}
            if hasattr(s,'forged'): result['candidate_id'] = s.forged
            yield ModelTextDelta(json.dumps(result,ensure_ascii=False))
    monkeypatch.setattr(main,'model_adapter',Model())
    async def receive(identifier='media', *, quote=False):
        data = payload(identifier, image=not quote,triggered=False)
        data['message'][0]['data']['text'] = 'happy'
        if quote:
            data['message'].insert(0, {'type':'reply','data':{'id':'group-quote'}})
            # Resolve quote to another member, not a quote of the bot (explicit).
            original = s.adapter._transport
            async def transport(operation, params):
                result = await original(operation, params)
                if operation=='get_msg': result['data']['sender']['user_id'] = '22'
                return result
            s.adapter._transport = transport
        event = await main.channel_host.accept_event(s.adapter.account_id,data)
        assert event is not None and not event.triggered
        await main._handle_channel_event(event)
        await asyncio.sleep(0)
        task = s.scheduler.tasks.get(s.thread.thread_id)
        await s.clock.advance(1)
        if task is not None:
            await asyncio.wait_for(task,5)
    s.media_receive = receive
    return s


@pytest.mark.parametrize('text', ['', '开心'])
@pytest.mark.parametrize('uncertain', [False, True])
def test_single_original_gif_message_and_actual_history(media_turn, text, uncertain):
    s = media_turn
    s.text, s.uncertain = text, uncertain
    async def run():
        try:
            await s.media_receive()
            assert len(s.calls)==1 and s.downloads==['https://qq.test/image']
            user = next(item for item in s.calls[0][0] if item['role']=='user')
            parts = user['content']
            pictures = [part for part in parts if part['type']=='image_url']
            assert len(pictures)==2
            for picture in pictures:
                image=Image.open(io.BytesIO(base64.b64decode(picture['image_url']['url'].split(',',1)[1])))
                assert image.size==(8,7)
            sends = [params for op,params in s.operations if op=='send_group_msg']
            assert len(sends)==1
            segments=sends[0]['message']
            assert [part['type'] for part in segments]==(['text','image'] if text else ['image'])
            raw=base64.b64decode(segments[-1]['data']['file'].removeprefix('base64://'))
            assert raw==s.raw and Image.open(io.BytesIO(raw)).n_frames==2
            messages=s.sessions.list_messages(s.thread.session_key)
            assert len(messages)==1 and messages[0].role=='assistant'
            assert messages[0].metadata['deliveryStatus']==('uncertain' if uncertain else 'sent')
            assert messages[0].metadata['attachments'][0]['mediaType']=='image/gif'
            app=main._natural_media_application()
            counts=next(iter(app.usage.values()))
            assert counts['attempts']==1 and counts['successes']==int(not uncertain)
            assert not list(app.root.iterdir())
            # Input GIF did not enter permanent intake; only one chosen library ref.
            assert len(list((s.roles.workspace_path(s.role.id)/'attachments').glob('*.json')))==1
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())


@pytest.mark.parametrize('verified', [False, True])
def test_real_adapter_quoted_images_require_verified_group(media_turn, verified):
    s=media_turn
    s.choose=False
    s.quote_group=100 if verified else None
    async def run():
        try:
            await s.media_receive(quote=True)
            assert s.downloads==(['https://qq.test/image'] if verified else [])
            assert len(s.calls)==1
            assert len([op for op,_ in s.operations if op=='send_group_msg'])==1
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['replace','disable','clear','forged'])
def test_predispatch_material_or_context_change_cannot_send_old_image(media_turn, change):
    s=media_turn
    s.text='看这张图'
    def change_material():
        if change=='replace': (s.root/'happy'/'original.gif').write_bytes(image_bytes('PNG'))
        elif change=='disable':
            document=json.loads((s.root/'manifest.json').read_text())
            document['categories']['happy']['enabled']=False
            (s.root/'manifest.json').write_text(json.dumps(document))
        elif change=='clear': s.channels.listening.clear(s.role.id,s.thread.thread_id)
    s.before_result=change_material
    if change=='forged': s.forged='/tmp/arbitrary.gif'
    async def run():
        try:
            await s.media_receive()
            sends=[params for op,params in s.operations if op=='send_group_msg']
            assert all([part['type'] for part in send['message']]==['text'] for send in sends)
            assert not sends
            assert not main._natural_media_application().usage
            assert not list(main._natural_media_application().root.iterdir())
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['no_vision','download_failure','bad_image'])
def test_unreadable_input_has_explicit_image_fact_not_invented_pixels(media_turn, monkeypatch, reason):
    s=media_turn
    s.choose=False
    if reason=='no_vision': monkeypatch.setattr(main.model_adapter_registry,'supports_images',lambda c:False)
    elif reason=='download_failure': s.download_error=True
    else: s.raw=b'not an image'
    async def run():
        try:
            await s.media_receive()
            user=next(item for item in s.calls[0][0] if item['role']=='user')
            content=user['content']
            assert isinstance(content,str)
            fact=json.loads(content)
            assert fact['hasImage'] is True and fact['imageReadable'] is False
            assert len([op for op,_ in s.operations if op=='send_group_msg'])==1
            assert not s.downloads if reason=='no_vision' else s.downloads
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())


def test_long_multiline_natural_image_reply_never_forwards_or_mentions(media_turn):
    s=media_turn
    s.text=('hello\n'*100)+'x'*400
    async def run():
        try:
            await s.media_receive()
            assert [op for op,_ in s.operations]==['send_group_msg']
            parts=s.operations[0][1]['message']
            assert [part['type'] for part in parts]==['text','image']
            assert len(parts[0]['data']['text'])<=300
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())


def test_library_change_during_image_encoding_stops_last_safe_handoff(media_turn, monkeypatch):
    s=media_turn
    original=s.adapter._image_segment
    async def revoke(attachment):
        result=await original(attachment)
        (s.root/'happy'/'original.gif').unlink()
        return result
    monkeypatch.setattr(s.adapter,'_image_segment',revoke)
    async def run():
        try:
            await s.media_receive()
            assert not [op for op,_ in s.operations if op=='send_group_msg']
            counts=next(iter(main._natural_media_application().usage.values()))
            assert counts['attempts']==1 and counts['successes']==0
            messages=s.sessions.list_messages(s.thread.session_key)
            assert len(messages)==1 and messages[0].metadata['deliveryStatus']=='failed'
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())


def test_recent_uncertain_attempt_prevents_same_meme_after_service_restart(media_turn):
    s=media_turn
    s.uncertain=True
    async def run():
        try:
            await s.media_receive('first')
            message=s.sessions.list_messages(s.thread.session_key)[0]
            assert message.metadata['naturalMemeSha256']
            main._natural_media_application._instance=None
            await s.clock.advance(4)  # scheduler cooldown elapsed, sticker cooldown has not
            s.text = '新的文字回应'
            await s.media_receive('second')
            sends=[params for op,params in s.operations if op=='send_group_msg']
            assert len(sends)==2
            assert [part['type'] for part in sends[1]['message']]==['text']
            assert len([send for send in sends if any(part['type']=='image' for part in send['message'])])==1
        finally:
            if main._proactive is not None: await main._proactive.close()
    asyncio.run(run())
