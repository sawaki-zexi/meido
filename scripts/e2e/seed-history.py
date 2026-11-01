"""Synthetic long-history data for the isolated integration checkout."""
import sys
import json
import sqlite3
from pathlib import Path

root = Path(sys.argv[1]).resolve()
source = Path(__file__).resolve().parents[2]
assert root.is_relative_to(source / '.data/chat-e2e/runs'), 'Only a disposable E2E workspace may be seeded'
marker = json.loads((root / '.meido-e2e.json').read_text(encoding='utf-8'))
assert marker['kind'] == 'meido-chat-e2e' and Path(marker['root']).resolve() == root
assert Path(marker['sourceRoot']).resolve() == source
database = root / '.data/development/sessions.db'
assert database.is_file()
with sqlite3.connect(database) as connection:
    assert connection.execute('SELECT COUNT(*) FROM agent_runs').fetchone()[0] == 0, 'Seed before any accepted input'
sys.path.insert(0, str(root))
from backend.app.session_store import SessionStore

store = SessionStore(database)
for role_id in sys.argv[2:]:
    store.open_role_session(role_id)
    key = 'role:' + role_id
    assert not store.list_messages(key), 'Fixture role must be new'
    for index in range(80):
        store.append_message(key, 'user', f'合成历史 {index}：' + '过去的对话。' * 35)
        store.append_message(key, 'assistant', f'合成回复 {index}：' + '此前的回复。' * 35,
                             metadata={'fixture': 'isolated-browser-acceptance', 'padding': 'x' * 1100})
    print(f'{role_id}: 160 synthetic messages')
