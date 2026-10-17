"""Accepted human input seals old CG before owned network cleanup finishes."""
import asyncio
import threading
import time

from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


def test_new_human_reply_does_not_wait_for_auxiliary_finally_but_next_cg_does(scene_main, monkeypatch):
    from backend.app import main
    client, service, peer, _ = scene_main
    peer.aux_block = True
    finally_entered, finally_release = threading.Event(), threading.Event()
    routed = main.model_adapter
    class HeldFirstAuxiliary:
        def __getattr__(self, name):
            return getattr(routed, name)
        async def stream_rich_messages(self, *args, **kwargs):
            first = not peer.aux_accepted.is_set()
            try:
                async for event in routed.stream_rich_messages(*args, **kwargs):
                    yield event
            finally:
                if first:
                    finally_entered.set()
                    await asyncio.to_thread(finally_release.wait)
    monkeypatch.setattr(main, 'model_adapter', HeldFirstAuxiliary())
    try:
        first = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'The old garden candidate.'})
        assert 'assistant_completed' in first.text and peer.aux_accepted.wait(2)
        started = time.monotonic()
        second = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'A new real owner turn.'})
        assert 'assistant_completed' in second.text and time.monotonic() - started < 2
        assert finally_entered.wait(2)
        assert not peer.aux_second.wait(.25), 'next CG started before the previous owned finally returned'
        assert not service.peer.requests
        finally_release.set()
        peer.aux_release.set()
        assert peer.aux_second.wait(3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            page = client.get(f'/api/roles/{service.role.id}/generations').json()
            if any(item['deliveryStatus'] == 'sent' for item in page['generations']):
                break
            time.sleep(.02)
        assert len(page['generations']) == 2 and page['usage']['cgAttempts'] == 2
        assert sum(item['deliveryStatus'] == 'sent' for item in page['generations']) == 1
        assert len(service.peer.requests) == 1
    finally:
        finally_release.set()
        peer.aux_release.set()


def test_new_human_acceptance_suppresses_a_received_late_image_and_waits_before_next_cg(scene_main, monkeypatch):
    from backend.app.novelai_generation import application
    client, service, peer, _ = scene_main
    importing, release_import = threading.Event(), threading.Event()
    original = application.import_response
    calls = []
    def held_import(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            importing.set()
            release_import.wait(8)
        return original(*args, **kwargs)
    monkeypatch.setattr(application, 'import_response', held_import)
    try:
        first = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'Old garden image.'})
        assert 'assistant_completed' in first.text and importing.wait(3)
        peer.observation.update(transition='changed', scene_key='house', visual_key='house-day')
        second = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'New house scene.'})
        assert 'assistant_completed' in second.text
        assert not peer.aux_second.wait(.25)
        release_import.set()
        assert peer.aux_second.wait(3)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            page = client.get(f'/api/roles/{service.role.id}/generations').json()
            if any(item['deliveryStatus'] == 'sent' for item in page['generations']):
                break
            time.sleep(.02)
        assert len(page['generations']) == 2 and len(service.peer.requests) == 2
        old = next(item for item in page['generations'] if item['parameters']['scene_key'] == 'garden')
        new = next(item for item in page['generations'] if item['parameters']['scene_key'] == 'house')
        assert old['attachments'] and old['deliveryStatus'] == 'suppressed'
        assert new['deliveryStatus'] == 'sent'
        rows = service.sessions.list_messages(f'role:{service.role.id}')
        assert len([row for row in rows if row.messageType == 'internal_completion']) == 1
        assert rows[-1].metadata['generationAttemptIds'] == [new['attemptId']]
    finally:
        release_import.set()
