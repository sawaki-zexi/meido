"""Accepted human input seals old CG before owned network cleanup finishes."""
import asyncio
import threading
import time
import pytest

from novelai_scene_fixtures import scene_main
from novelai_fixtures import novelai_environment, novelai_main, novelai_service  # noqa: F401


@pytest.mark.parametrize('change', ['parameters', 'token', 'role_aba'])
def test_waiting_cg_candidate_cannot_inherit_later_settings_but_new_turn_can(scene_main, change):
    from test_novelai_scene import owner_turn
    client, service, peer, _ = scene_main
    app = service.application
    key = service.role.id, f'role:{service.role.id}'

    async def hold_cleanup():
        gate = asyncio.Event()
        app.scenes.cleanup[key] = {asyncio.create_task(gate.wait())}
        return gate

    gate = client.portal.call(hold_cleanup)
    try:
        response = client.post(f'/api/roles/{service.role.id}/messages', json={'content': 'The old waiting scene.'})
        assert 'assistant_completed' in response.text, response.text
        candidate = app.scenes.pending[key]
        assert not peer.aux_accepted.is_set() and not app.history(service.role.id)['generations']
        if change == 'parameters':
            current = client.get('/api/services/novelai').json()
            changed = client.put('/api/services/novelai', json={'maxSteps': 12, 'expectedRevision': current['revision']})
            assert changed.status_code == 200, changed.text
        elif change == 'token':
            changed = client.put('/api/services/novelai/token', json={'token': 'new-local-fixture-token'})
            assert changed.status_code == 200, changed.text
        else:
            for enabled in (False, True):
                changed = client.put(f'/api/roles/{service.role.id}', json={'name': service.role.name,
                    'profile': service.role.profile.model_dump(), 'agentConfig': {'novelai': {'sceneCgEnabled': enabled}}})
                assert changed.status_code == 200, changed.text
        client.portal.call(gate.set)

        async def joined_candidate():
            await asyncio.wait_for(asyncio.gather(candidate, return_exceptions=True), 3)

        client.portal.call(joined_candidate)
        assert not peer.aux_accepted.is_set(), 'an old candidate acquired a new auxiliary request'
        assert not service.peer.requests
        assert not app.history(service.role.id)['generations']
        generated, _ = owner_turn(client, service, 'A new owner turn after the settings change.')
        assert generated['status'] == 'succeeded'
        assert len(service.peer.requests) == 1
        if change == 'parameters':
            assert generated['parameters']['steps'] == 12
    finally:
        client.portal.call(gate.set)


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
