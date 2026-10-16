"""Real owner HTTP intake seals a channel tick before its held response joins."""
import asyncio
from contextlib import closing, contextmanager
import json
import socket
import sqlite3
import threading

import httpx
import pytest
import uvicorn

from backend.app import main
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextDelta, ModelToolCallBlock, ModelTextBlock
from backend.app.channels.napcat import NapCatAdapter, NapCatConfig
from backend.app.proactive_runtime.identity import AutonomyOrigin
from test_mcp_http import process_alive
from test_proactive_channel_guards import decision
from test_proactive_targets_api import api, shell_api, platform, source  # noqa: F401


pytestmark = pytest.mark.usefixtures('local_platform_only')


@contextmanager
def owner_http():
    """A persistent ASGI loop keeps cleanup alive after the owner HTTP returns."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    ready, errors, loops = threading.Event(), [], []
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan='off', log_level='critical', ws='none'))

    async def serve():
        loops.append(asyncio.get_running_loop())
        ready.set()
        await server.serve(sockets=[listener])

    def host():
        try:
            asyncio.run(serve())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=host, name='proactive-owner-http')
    thread.start()
    try:
        assert ready.wait(10), 'owner HTTP loop did not start'
        for _ in range(1000):
            if server.started or errors:
                break
            threading.Event().wait(.01)
        assert server.started and not errors, errors

        def invoke(operation):
            return asyncio.run_coroutine_threadsafe(operation, loops[0]).result(timeout=30)

        with httpx.Client(base_url=f'http://127.0.0.1:{listener.getsockname()[1]}', timeout=20) as client:
            yield client, invoke
    finally:
        server.should_exit = True
        thread.join(15)
        try:
            assert not thread.is_alive(), 'owner HTTP server still owns its event loop'
            assert not errors, errors
        finally:
            listener.close()


@pytest.mark.parametrize('bind_runtime', [False, True], ids=['local-fixture', 'bound-host-runtime'])
def test_real_owner_http_returns_before_original_channel_response_cleanup_and_next_tick_waits(
    api, monkeypatch, platform, source, bind_runtime,
):
    received, cleaning, release, cleaned = (threading.Event() for _ in range(4))
    observed, events = [], []
    old_origin = []

    with decision(api, monkeypatch, platform, source) as ctx:
        app, role = ctx['app'], ctx['role']

        async def held_rpc(action, params):
            result = await platform[1](action, params)
            if action == 'send_private_msg':
                # The real localhost RPC already received message_id=8301.
                # Hold that response at the external transport boundary;
                # the adapter/host has not received a definitive local result.
                observed.append(result)
                events.append('platform_received')
                received.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    events.append('cleanup_entered')
                    cleaning.set()
                    try:
                        while not release.is_set():
                            await asyncio.sleep(.01)
                    finally:
                        events.append('cleanup_exited')
                        cleaned.set()
            return result

        main.channel_host.register(NapCatAdapter(NapCatConfig(ctx['account'].account_id), transport=held_rpc))

        class Model:
            rich_requests = 0
            human_requests = 0

            async def stream_rich_messages(self, messages, configuration, tools=(), *, max_tokens=None, cancel_check=None):
                if 'real-owner-input-before-release' in str(messages):
                    self._human_reply(messages)
                    yield ModelStreamDone(ModelAssistantMessage(content=(ModelTextBlock('real-owner-reply-before-release'),)))
                    return
                self.rich_requests += 1
                if self.rich_requests == 1:
                    name, arguments = 'get_content_events', {}
                elif self.rich_requests == 2:
                    item = json.loads(messages[-1].content[0].text)[0]['id']
                    name, arguments = 'message_push', {'message': 'old-original-channel-once', 'mood': '',
                        'thought': '', 'evidence': [item]}
                else:
                    assert self.rich_requests == 3, 'a sealed old tick made another model request'
                    name, arguments = 'finish_turn', {'decision': 'reply'}
                yield ModelStreamDone(ModelAssistantMessage(content=(
                    ModelToolCallBlock(f'human-priority-{self.rich_requests}', name, arguments),)))

            def _human_reply(self, messages):
                self.human_requests += 1
                assert 'real-owner-input-before-release' in str(messages)
                assert received.is_set() and not release.is_set()
                assert old_origin and not app._authorized(old_origin[0])
                assert app.query(role)['humanBusy'] is True
                events.append('human_reply')

            async def stream_structured_messages(self, messages, configuration, tools):
                self._human_reply(messages)
                yield ModelTextDelta('real-owner-reply-before-release')

        model = Model()
        monkeypatch.setattr(main, 'model_adapter', model)

        with owner_http() as (client, invoke):
            try:
                async def bind_host():
                    if bind_runtime:
                        main.channel_host.bind_runtime_loop()
                invoke(bind_host())
                ctx['now'][0] += 1800
                assert invoke(app.on_clock()) == 1
                assert received.wait(15), 'original localhost send never reached the response boundary'
                assert observed == [{'message_id': 8301}]
                assert len(platform[0]['sent']) == 1 and platform[0]['sent'][0]['user_id'] == 101
                initial = app.query(role)
                assert len(initial['runs']) == 1 and model.rich_requests == 3
                frozen = initial['runs'][0]['origin']
                old_origin.append(AutonomyOrigin(frozen['roleId'], frozen['tickId'], frozen['grantRevision'],
                    frozen['generation'], tuple(frozen['allowedModes'])))
                assert app._authorized(old_origin[0])

                response = client.post(f'/api/roles/{role}/messages', json={'content': 'real-owner-input-before-release'})
                assert response.status_code == 200, response.text
                assert 'assistant_completed' in response.text and 'real-owner-reply-before-release' in response.text
                events.append('human_http_completed')
                # Completion while release is still unset is the time proof;
                # no narrow response-time threshold substitutes for this gate.
                assert not release.is_set() and not cleaned.is_set()
                assert cleaning.wait(10), 'old cancellation did not enter owned transport cleanup'
                assert not app._authorized(old_origin[0])
                held = app.query(role)
                assert held['cleanupPending'] is True and held['humanBusy'] is False
                assert model.human_requests == 1 and model.rich_requests == 3
                assert len(held['runs']) == 1 and not held['runs'][0]['runtimeAcknowledged']
                ctx['now'][0] += 1800
                assert invoke(app.on_clock()) == 0
                assert app.query(role)['cleanupPending'] is True
                assert len(app.query(role)['runs']) == 1
                assert model.rich_requests == 3 and len(platform[0]['sent']) == 1
                calls = [json.loads(line) for line in (source[0] / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
                assert [call['name'] for call in calls] == ['get_events']

                events.append('release_set')
                release.set()

                async def settle():
                    async with asyncio.timeout(20):
                        while True:
                            await app.recover()
                            state = app.query(role)
                            if (state['runs'] and all(row['runtimeAcknowledged'] for row in state['runs'])
                                    and not state['cleanupPending'] and not app.has_owned_work):
                                await app.recover()
                                return app.query(role)
                            await asyncio.sleep(.01)

                final = invoke(settle())
                assert cleaned.is_set()
                assert events.index('platform_received') < events.index('human_reply') < events.index('human_http_completed')
                assert events.index('human_http_completed') < events.index('release_set') < events.index('cleanup_exited')
                assert len(final['runs']) == 1 and final['runs'][0]['status'] == 'aborted'
                assert final['runs'][0]['runtimeAcknowledged'] is True
                assert not final['cleanupPending'] and not final['humanBusy']
                assert not app._authorized(old_origin[0])
                assert len(final['deliveries']) == 1 and final['acknowledgements'] == []
                delivery = final['deliveries'][0]
                assert delivery['status'] in {'pending', 'uncertain'}
                assert delivery['attemptId'] and delivery['platformMessageId'] is None and delivery['messageId'] is None
                assert delivery['projectionPending'] is True and delivery['target']['ref'] == ctx['selected']['targetRef']
                attempts = ctx['channels'].list_deliveries(role)
                assert len(attempts) == 1 and attempts[0]['status'] in {'pending', 'uncertain'}
                assert len(platform[0]['sent']) == 1 and observed == [{'message_id': 8301}]
                assert model.rich_requests == 3 and model.human_requests == 1
                calls_after = [json.loads(line) for line in (source[0] / 'calls.jsonl').read_text(encoding='utf-8').splitlines()]
                assert calls_after == calls
                assert not process_alive(int((source[0] / 'pid').read_text()))
                for channel_thread in ctx['channels'].list_threads(role):
                    assert ctx['channels'].list_messages(role, channel_thread.thread_id)[1] == []
                desktop = api[2].list_messages(f'role:{role}')
                assert [(message.role, message.content) for message in desktop] == [
                    ('user', 'real-owner-input-before-release'), ('assistant', 'real-owner-reply-before-release')]
                human_run = api[2].get_run(desktop[0].runId)
                assert human_run.status == 'completed' and human_run.modelSnapshot['humanInputSource'] == 'desktop_owner'
                with closing(sqlite3.connect(api[2].database_path)) as db:
                    row = db.execute('SELECT runtime_result,result_ack_pending,runtime_acknowledged FROM autonomy_runs WHERE tick_id=?',
                        (old_origin[0].tick_id,)).fetchone()
                    assert row[0] is not None and row[1:] == (0, 1)
                with closing(sqlite3.connect(api[3].root / 'tasks.db')) as db:
                    row = db.execute('SELECT ended_at,retain_until_ack FROM tasks WHERE task_id=?', (final['runs'][0]['taskId'],)).fetchone()
                    assert row is not None and row[0] is not None and row[1] == 0
                    assert db.execute('SELECT COUNT(*) FROM tasks WHERE role_id=? AND retain_until_ack<>0', (role,)).fetchone()[0] == 0
            finally:
                # Unblock the external boundary before any close or server join,
                # including assertion/HTTP timeout failures. The real owners join.
                release.set()
                try:
                    invoke(app.close())
                finally:
                    try:
                        invoke(main.channel_host.stop(ctx['account'].account_id))
                    finally:
                        invoke(main.mcp_host.close())
