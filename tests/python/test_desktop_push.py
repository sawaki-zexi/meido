"""Desktop delivery facts survive retries and obey the final commit boundary."""
import asyncio
import os
import sqlite3
from threading import Event, Timer

import pytest

from backend.app.agent_runtime import (
    AssistantDoneEvent, AssistantMessage, CancellationToken, ToolCall,
    ToolCallEndEvent, ToolContext, UserMessage, run_agent_loop,
)
from backend.app.chat_attachments import save_attachment
from backend.app.desktop_push import MessagePushTool
from backend.app.storage import initialize_databases
from backend.app.session_store import SessionStore


@pytest.fixture(autouse=True)
def database(tmp_path):
    initialize_databases(tmp_path)


def context(role='role', run='run', call='call', signal=None):
    return ToolContext(role, f'role:{role}', run, signal or CancellationToken(), call)


def test_replayed_host_call_and_restart_keep_one_message_but_other_origins_are_distinct(tmp_path):
    path = tmp_path / 'sessions.db'
    messages = []
    for role, run, call in [('role', 'run', 'call'), ('role', 'run', 'call'),
                            ('role', 'run-2', 'call'), ('role', 'run', 'call-2'), ('other', 'run', 'call')]:
        sessions = SessionStore(path)
        tool = MessagePushTool(sessions, lambda: tmp_path, role_id=role, authorized=lambda _: True)
        result = asyncio.run(tool.execute({'channel': 'desktop', 'chat_id': 'owner', 'message': 'durable'}, context(role, run, call)))
        assert not result.is_error
        messages.append(result.details['messageId'])
    assert messages[0] == messages[1]
    assert len(set(messages)) == 4
    assert len(SessionStore(path).list_messages('role:role')) == 3
    assert len(SessionStore(path).list_messages('role:other')) == 1


@pytest.mark.parametrize('change', ['cancel', 'revoke', 'media_deleted'])
@pytest.mark.parametrize('cancel_delay,unlock_delay', [(.01, .02), (.1, .4)])
def test_parent_runtime_cannot_commit_after_revocation_while_waiting_for_sqlite(tmp_path, change, cancel_delay, unlock_delay):
    sessions = SessionStore(tmp_path / 'sessions.db')
    attachment = save_attachment(tmp_path, 'push.txt', 'text/plain', b'owned-data')
    blocker = sqlite3.connect(sessions.database_path, check_same_thread=False)
    blocker.execute('BEGIN IMMEDIATE')
    parent, allowed, changed, timers = CancellationToken(), [True], Event(), []

    def revoke():
        if change == 'cancel':
            parent.cancel()
        elif change == 'revoke':
            allowed[0] = False
        else:
            from pathlib import Path
            Path(str(attachment['path'])).unlink()
        changed.set()

    def unlock():
        assert changed.wait(5)
        blocker.commit()

    def authorized(_context):
        if not timers:
            for delay, operation in ((cancel_delay, revoke), (unlock_delay, unlock)):
                timer = Timer(delay, operation)
                timers.append(timer)
                timer.start()
        return allowed[0]

    tool = MessagePushTool(sessions, lambda: tmp_path, role_id='role', authorized=authorized)
    call = ToolCall('push', 'message_push', {'channel': 'desktop', 'chat_id': 'owner', 'file': attachment['id']})
    class Provider:
        async def stream_response(self, **kwargs):
            if any(getattr(item, 'tool_name', '') == 'message_push' for item in kwargs['messages']):
                yield AssistantDoneEvent(AssistantMessage(content='done'))
            else:
                yield ToolCallEndEvent(call)
                yield AssistantDoneEvent(AssistantMessage(tool_calls=(call,), stop_reason='tool_use'))
    async def run():
        return [event async for event in run_agent_loop(provider=Provider(), model='fixture', system='',
            messages=[UserMessage('push')], tools=[tool], signal=parent,
            role_id='role', session_key='role:role', run_id='run')]
    try:
        asyncio.run(run())
    finally:
        for timer in timers:
            timer.join(5)
        blocker.close()
    assert changed.is_set()
    assert sessions.list_messages('role:role') == []


def test_repeated_task_cancel_keeps_the_sqlite_worker_owned_until_it_finishes(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path / 'sessions.db')
    started, released, finished = Event(), Event(), Event()
    original = sessions.append_desktop_push_once
    def append(*args, **kwargs):
        started.set()
        assert released.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(sessions, 'append_desktop_push_once', append)
    async def run():
        tool = MessagePushTool(sessions, lambda: tmp_path, role_id='role', authorized=lambda _: True)
        pending = asyncio.create_task(tool.execute({'channel': 'desktop', 'chat_id': 'owner', 'message': 'no late write'}, context()))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            for _ in range(3):
                pending.cancel()
                await asyncio.sleep(.01)
                assert not pending.done() and not finished.is_set()
        finally:
            released.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finished.is_set()
    asyncio.run(run())
    assert sessions.list_messages('role:role') == []


def test_media_only_message_has_a_nonempty_safe_provider_history(tmp_path):
    sessions = SessionStore(tmp_path / 'sessions.db')
    attachment = save_attachment(tmp_path, 'push.txt', 'text/plain', b'private-file-body')
    tool = MessagePushTool(sessions, lambda: tmp_path, role_id='role', authorized=lambda _: True)
    result = asyncio.run(tool.execute({'channel': 'desktop', 'chat_id': 'owner', 'file': attachment['id']}, context()))
    assert not result.is_error
    message = sessions.runtime_messages('role:role')[0]
    assert message.content and 'push.txt' in message.content and attachment['id'] in message.content
    assert 'private-file-body' not in message.content and str(tmp_path) not in message.content


@pytest.mark.skipif(os.name != 'nt', reason='Windows attachment-directory junction')
def test_attachment_directory_cannot_redirect_to_another_roles_media(tmp_path):
    import subprocess
    sessions = SessionStore(tmp_path / 'sessions.db')
    foreign = tmp_path / 'foreign'
    attachment = save_attachment(foreign, 'foreign.txt', 'text/plain', b'foreign-private-body')
    workspace = tmp_path / 'own'
    workspace.mkdir()
    link = workspace / 'attachments'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(foreign / 'attachments')],
                   capture_output=True, check=True)
    try:
        tool = MessagePushTool(sessions, lambda: workspace, role_id='role', authorized=lambda _: True)
        result = asyncio.run(tool.execute({'channel': 'desktop', 'chat_id': 'owner', 'file': attachment['id']}, context()))
        assert result.is_error and sessions.list_messages('role:role') == []
        assert 'foreign-private-body' not in result.content
    finally:
        os.rmdir(link)


@pytest.mark.parametrize('change', ['signal', 'permission', 'task'])
def test_final_commit_rechecks_permission_after_attachment_resolution(tmp_path, change):
    sessions = SessionStore(tmp_path / 'sessions.db')
    attachment = save_attachment(tmp_path, 'push.txt', 'text/plain', b'owned')
    entered, release = Event(), Event()
    reads, allowed, signal = [0], [True], CancellationToken()
    def workspace():
        reads[0] += 1
        if reads[0] == 2:
            entered.set()
            assert release.wait(5)
        return tmp_path
    tool = MessagePushTool(sessions, workspace, role_id='role', authorized=lambda _: allowed[0])
    async def run():
        pending = asyncio.create_task(tool.execute({'channel': 'desktop', 'chat_id': 'owner', 'file': attachment['id']}, context(signal=signal)))
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            if change == 'signal':
                signal.cancel()
            elif change == 'permission':
                allowed[0] = False
            else:
                pending.cancel()
                await asyncio.sleep(0)
        finally:
            release.set()
        if change == 'task':
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            assert (await pending).is_error
    asyncio.run(run())
    assert sessions.list_messages('role:role') == []


def test_stop_after_commit_gate_waits_for_the_authorized_write_and_preserves_it(tmp_path, monkeypatch):
    sessions = SessionStore(tmp_path / 'sessions.db')
    entered, release = Event(), Event()
    connect = sqlite3.connect
    def traced_connection(*args, **kwargs):
        connection = connect(*args, **kwargs)
        def trace(statement):
            if statement.startswith('INSERT INTO messages'):
                entered.set()
                release.wait(5)
        connection.set_trace_callback(trace)
        return connection
    monkeypatch.setattr(sqlite3, 'connect', traced_connection)
    tool = MessagePushTool(sessions, lambda: tmp_path, role_id='role', authorized=lambda _: True)
    async def run():
        pending = asyncio.create_task(tool.execute({'channel': 'desktop', 'chat_id': 'owner', 'message': 'gate already won'}, context()))
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            pending.cancel()
            await asyncio.sleep(.01)
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
    asyncio.run(run())
    messages = sessions.list_messages('role:role')
    assert len(messages) == 1 and messages[0].content == 'gate already won' and messages[0].status == 'completed'
