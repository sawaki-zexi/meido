import sqlite3

import pytest

from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_coding_completion_is_unique_internal_and_requires_existing_original_session(tmp_path):
    initialize_databases(tmp_path / 'data')
    sessions = SessionStore(tmp_path / 'data/sessions.db')
    sessions.open_role_session('role-a')
    with sqlite3.connect(sessions.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        message = sessions.append_coding_completion_once('role-a', 'role:role-a', 'task-a', 'attempt-a', '结果',
            authorized=lambda: True, connection=db, delivery_status='sent')
    with sqlite3.connect(sessions.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        repeated = sessions.append_coding_completion_once('role-a', 'role:role-a', 'task-a', 'attempt-a', '结果',
            authorized=lambda: True, connection=db, delivery_status='sent')
    assert repeated.id == message.id
    assert message.metadata['internalEvent'] == 'coding_completion'
    assert message.messageType == 'internal_completion'
    assert message.role == 'assistant'
    assert len(sessions.list_messages('role:role-a')) == 1
    with sqlite3.connect(sessions.database_path) as db:
        with pytest.raises(PermissionError):
            sessions.append_coding_completion_once('role-a', 'role:missing', 'task-a', 'attempt-b', '结果',
                authorized=lambda: True, connection=db)
        with pytest.raises(PermissionError):
            sessions.append_coding_completion_once('role-a', 'role:role-a', 'task-a', 'attempt-b', '结果',
                authorized=lambda: False, connection=db)
    assert sessions.list_messages('role:missing') == []
