"""A generated attachment is consumed with the exact final reply transaction."""
from datetime import datetime, timezone
import sqlite3

import pytest
from novelai_fixtures import novelai_environment  # noqa: F401

from backend.app.models import AgentRun
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


@pytest.fixture
def reply(tmp_path):
    initialize_databases(tmp_path)
    store = SessionStore(tmp_path / "sessions.db")
    session = store.open_role_session("role")
    run = AgentRun(runId="human-run", roleId="role", sessionKey=session.sessionKey,
        status="created", modelSnapshot={"humanInputSource": "desktop_owner"},
        startedAt=datetime.now(timezone.utc))
    _, _, placeholder = store.create_run_with_messages(run, "Draw a local fixture")
    _, final = store.finish_run(run.runId, status="completed", assistant_message_id=placeholder.id,
        assistant_content="Original final reply", turn_count=1)
    attachment = {"id": "generated-image", "name": "image.png", "mediaType": "image/png", "size": 10}
    return store, session.sessionKey, run.runId, final, attachment


def attach(reply, *, connection, authorized=lambda: True, **changes):
    store, session_key, run_id, final, attachment = reply
    arguments = dict(role_id="role", session_key=session_key, run_id=run_id,
                     message_id=final.id, attempt_id="generation-attempt", attachments=[attachment])
    arguments.update(changes)
    return store.append_generation_attachments_once(**arguments, connection=connection, authorized=authorized)


def test_attachment_and_outbox_share_rollback_and_repeated_consumption_is_once(reply):
    store, session_key, _, final, attachment = reply
    with pytest.raises(RuntimeError, match="outbox"):
        with sqlite3.connect(store.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            attach(reply, connection=db)
            raise RuntimeError("outbox update failed")
    assert next(message for message in store.list_messages(session_key) if message.id == final.id).metadata.get("attachments") is None
    with sqlite3.connect(store.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        first = attach(reply, connection=db)
        second = attach(reply, connection=db)
        assert first == second
    stored = next(message for message in store.list_messages(session_key) if message.id == final.id)
    assert stored.content == final.content and stored.status == final.status
    assert stored.metadata["attachments"] == [attachment]
    assert stored.metadata["generationAttemptIds"] == ["generation-attempt"]


@pytest.mark.parametrize("change", [dict(role_id="different-role"), dict(session_key="role:other"),
                                      dict(run_id="other-run"), dict(message_id="missing-reply")])
def test_cross_role_run_session_or_message_cannot_consume(reply, change):
    store = reply[0]
    with sqlite3.connect(store.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(PermissionError):
            attach(reply, connection=db, **change)


def test_revocation_is_checked_even_when_replaying_a_consumed_attempt(reply):
    store = reply[0]
    with sqlite3.connect(store.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        attach(reply, connection=db)
        with pytest.raises(PermissionError):
            attach(reply, connection=db, authorized=lambda: False)


def test_ui_completion_rechecks_authority_after_session_lookup_before_inserting(reply):
    store, session_key, _, _, attachment = reply
    checks = iter([True, True, False])
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(PermissionError):
            store.append_generation_completion_once('role', session_key, 'ui-final-revocation', [attachment],
                connection=db, authorized=lambda: next(checks))
    assert not any(message.messageType == 'internal_completion' for message in store.list_messages(session_key))


def test_foreign_or_unowned_transaction_is_rejected(reply, tmp_path):
    store = reply[0]
    with sqlite3.connect(store.database_path) as db:
        with pytest.raises(ValueError, match="transaction"):
            attach(reply, connection=db)
    with sqlite3.connect(tmp_path / "foreign.db") as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="database"):
            attach(reply, connection=db)


def test_ui_generation_completion_is_once_and_never_creates_human_or_run(reply):
    store, key, _, _, attachment = reply
    before = store.list_messages(key)
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        first = store.append_generation_completion_once('role', key, 'ui-attempt', [attachment],
            connection=db, authorized=lambda: True)
        again = store.append_generation_completion_once('role', key, 'ui-attempt', [attachment],
            connection=db, authorized=lambda: True)
        assert first == again
    messages = store.list_messages(key)
    assert len(messages) == len(before) + 1
    completion = messages[-1]
    assert completion.role == 'assistant' and completion.runId is None
    assert completion.messageType == 'internal_completion'
    assert completion.metadata['internalEvent'] == 'novelai_generation'
    assert completion.metadata['attachments'] == [attachment]
    assert [m.id for m in messages if m.role == 'user'] == [m.id for m in before if m.role == 'user']


def test_existing_metadata_and_images_survive_append_and_final_revocation_rolls_back(reply):
    store, key, _, final, attachment = reply
    old = {'id': 'old-image', 'name': 'old.png', 'mediaType': 'image/png', 'size': 12}
    store.update_message(final.id, final.content, final.status, metadata={'attachments': [old], 'retained': {'value': 171}})
    checks = iter([True, False])
    with pytest.raises(PermissionError):
        with sqlite3.connect(store.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            attach(reply, connection=db, authorized=lambda: next(checks))
    assert store.list_messages(key)[-1].metadata == {'attachments': [old], 'retained': {'value': 171}}
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        attach(reply, connection=db)
    saved = store.list_messages(key)[-1]
    assert saved.metadata['attachments'] == [old, attachment]
    assert saved.metadata['retained'] == {'value': 171}


@pytest.mark.parametrize('change', ['tool_call', 'unfinished_run'])
def test_non_final_assistant_and_unfinished_run_cannot_receive_image(reply, change):
    store, key, run_id, final, _ = reply
    if change == 'tool_call':
        target = store.append_message(key, 'assistant', 'tool invocation', run_id=run_id, message_type='tool_call')
    else:
        run_id = 'unfinished-run'
        _, _, target = store.create_run_with_messages(AgentRun(runId=run_id, roleId='role', sessionKey=key,
            status='running', startedAt=datetime.now(timezone.utc)), 'another real input')
        store.update_message(target.id, 'reply completed but runtime still owns it', 'completed')
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(PermissionError):
            attach(reply, connection=db, run_id=run_id, message_id=target.id)
