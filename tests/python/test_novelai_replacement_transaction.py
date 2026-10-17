"""Replace only the selected generated image in its caller-owned transaction."""
import sqlite3
import pytest

from novelai_fixtures import novelai_environment  # noqa: F401
from test_novelai_reply_transaction import reply  # noqa: F401


def test_replace_selected_image_keeps_other_images_files_and_metadata_and_replays_once(reply):
    store, key, _, final, _ = reply
    before = {'id': 'first-image', 'name': 'first.png', 'mediaType': 'image/png', 'size': 20, 'kind': 'image'}
    selected = {'id': 'selected-image', 'name': 'selected.png', 'mediaType': 'image/png', 'size': 30, 'kind': 'image'}
    file = {'id': 'keep-file', 'name': 'notes.txt', 'mediaType': 'text/plain', 'size': 40, 'kind': 'file'}
    new = {'id': 'new-image', 'name': 'new.png', 'mediaType': 'image/png', 'size': 50, 'kind': 'image'}
    metadata = {'attachments': [before, selected, file], 'generationAttemptIds': ['original-attempt'],
                'keep': {'source': 'original', 'nested': [1, 2]}}
    store.update_message(final.id, final.content, final.status, metadata=metadata)
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        first = store.replace_attachment_once('role', key, final.id, selected['id'], new, 'regeneration-attempt',
            connection=db, authorized=lambda: True)
        second = store.replace_attachment_once('role', key, final.id, selected['id'], dict(reversed(list(new.items()))), 'regeneration-attempt',
            connection=db, authorized=lambda: True)
        assert first == second
    saved = store.list_messages(key)[-1]
    assert saved.metadata['attachments'] == [before, new, file]
    assert saved.metadata['keep'] == metadata['keep']
    assert saved.metadata['generationAttemptIds'] == ['original-attempt', 'regeneration-attempt']
    assert saved.content == final.content and saved.status == final.status


def test_replacement_of_a_changed_selected_slot_is_an_explicit_conflict(reply):
    from backend.app.session_store import AttachmentConflict

    store, key, _, final, _ = reply
    changed = {'id': 'other-image', 'name': 'changed.png', 'mediaType': 'image/png', 'size': 30}
    new = {'id': 'new-image', 'name': 'new.png', 'mediaType': 'image/png', 'size': 50}
    metadata = {'attachments': [changed], 'keep': 'unrelated metadata'}
    store.update_message(final.id, final.content, final.status, metadata=metadata)
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(AttachmentConflict):
            store.replace_attachment_once('role', key, final.id, 'expected-old-image', new, 'regeneration-attempt',
                connection=db, authorized=lambda: True)
    assert store.list_messages(key)[-1].metadata == metadata


def test_deleted_original_message_is_a_conflict_without_creating_another_message(reply):
    from backend.app.session_store import AttachmentConflict

    store, key, _, final, _ = reply
    new = {'id': 'new-image', 'name': 'new.png', 'mediaType': 'image/png', 'size': 50}
    with sqlite3.connect(store.database_path) as db:
        db.execute('DELETE FROM messages WHERE message_id=?', (final.id,))
    before = store.list_messages(key)
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(AttachmentConflict):
            store.replace_attachment_once('role', key, final.id, 'expected-old-image', new, 'regeneration-attempt',
                connection=db, authorized=lambda: True)
    assert store.list_messages(key) == before


def test_replacement_rolls_back_with_the_actual_canonical_outbox_write_failure(reply):
    from backend.app.novelai_generation.ledger import GenerationLedger

    store, key, run_id, final, _ = reply
    old = {'id': 'old-image', 'name': 'old.png', 'mediaType': 'image/png', 'size': 30}
    new = {'id': 'new-image', 'name': 'new.png', 'mediaType': 'image/png', 'size': 50}
    metadata = {'attachments': [old], 'keep': {'value': 172}}
    store.update_message(final.id, final.content, final.status, metadata=metadata)
    ledger = GenerationLedger(store.database_path)
    prepared = ledger.prepare(role_id='role', source_scope='desktop', human_key=run_id, request_key='new-owner-action',
        parameters={'mode': 'txt2img', 'seed': 172}, origin={'roleId': 'role', 'sessionKey': key},
        service_revision=1, role_revision='fixture', account_limit=20, role_limit=10)
    with sqlite3.connect(store.database_path) as db:
        db.execute("CREATE TRIGGER fail_replacement_outbox BEFORE UPDATE OF state ON novelai_media_outbox WHEN NEW.state='sent' BEGIN SELECT RAISE(ABORT,'controlled canonical outbox failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='canonical outbox'):
        with sqlite3.connect(store.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            store.replace_attachment_once('role', key, final.id, old['id'], new, prepared['attemptId'],
                connection=db, authorized=lambda: True)
            db.execute("UPDATE novelai_media_outbox SET state='sent',consumed=1 WHERE attempt_id=?", (prepared['attemptId'],))
    assert store.list_messages(key)[-1].metadata == metadata
    assert ledger.get('role', 'desktop', prepared['generationId'])['deliveryStatus'] == 'pending'


@pytest.mark.parametrize('change', ['role', 'session', 'user_message', 'failed_message'])
def test_replacement_cannot_change_foreign_or_nonfinal_message(reply, change):
    store, key, _, final, _ = reply
    old = {'id': 'old-image', 'name': 'old.png', 'mediaType': 'image/png', 'size': 30}
    new = {'id': 'new-image', 'name': 'new.png', 'mediaType': 'image/png', 'size': 50}
    store.update_message(final.id, final.content, 'failed' if change == 'failed_message' else final.status,
        metadata={'attachments': [old]})
    before = store.list_messages(key)
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(PermissionError):
            store.replace_attachment_once('foreign' if change == 'role' else 'role',
                'role:foreign' if change == 'session' else key,
                before[0].id if change == 'user_message' else final.id, old['id'], new, 'attempt',
                connection=db, authorized=lambda: True)
    assert store.list_messages(key) == before


def test_final_revocation_rolls_back_replacement_and_consumed_replay_still_needs_authority(reply):
    store, key, _, final, _ = reply
    old = {'id': 'old-image', 'name': 'old.png', 'mediaType': 'image/png', 'size': 30}
    new = {'id': 'new-image', 'name': 'new.png', 'mediaType': 'image/png', 'size': 50}
    store.update_message(final.id, final.content, final.status, metadata={'attachments': [old]})
    checks = iter([True, False])
    with pytest.raises(PermissionError):
        with sqlite3.connect(store.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            store.replace_attachment_once('role', key, final.id, old['id'], new, 'attempt',
                connection=db, authorized=lambda: next(checks))
    assert store.list_messages(key)[-1].metadata == {'attachments': [old]}
    with sqlite3.connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        store.replace_attachment_once('role', key, final.id, old['id'], new, 'attempt', connection=db, authorized=lambda: True)
        with pytest.raises(PermissionError):
            store.replace_attachment_once('role', key, final.id, old['id'], new, 'attempt', connection=db, authorized=lambda: False)


def test_two_new_attempts_competing_for_the_same_old_image_only_replace_once(reply):
    from concurrent.futures import ThreadPoolExecutor
    from backend.app.session_store import AttachmentConflict

    store, key, _, final, _ = reply
    old = {'id': 'old-image', 'name': 'old.png', 'mediaType': 'image/png', 'size': 30}
    store.update_message(final.id, final.content, final.status, metadata={'attachments': [old], 'keep': 172})

    def replace(identifier):
        new = {'id': identifier, 'name': 'new.png', 'mediaType': 'image/png', 'size': 50}
        try:
            with sqlite3.connect(store.database_path) as db:
                db.execute('BEGIN IMMEDIATE')
                store.replace_attachment_once('role', key, final.id, old['id'], new, identifier,
                    connection=db, authorized=lambda: True)
            return 'replaced', identifier
        except AttachmentConflict:
            return 'conflict', identifier

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(replace, ['first-new-image', 'second-new-image']))
    assert sorted(status for status, _ in results) == ['conflict', 'replaced']
    winner = next(identifier for status, identifier in results if status == 'replaced')
    saved = store.list_messages(key)[-1]
    assert saved.metadata['attachments'][0]['id'] == winner
    assert saved.metadata['generationAttemptIds'] == [winner]
    assert saved.metadata['keep'] == 172
