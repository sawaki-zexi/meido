"""Inspect or seed only the disposable workspace owned by chat-e2e.mjs."""
import json
import sqlite3
import sys
from pathlib import Path

root, action, role = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
source = Path(__file__).resolve().parents[2]
assert root.is_relative_to(source / '.data/chat-e2e/runs')
marker = json.loads((root / '.meido-e2e.json').read_text(encoding='utf-8'))
assert marker['kind'] == 'meido-chat-e2e' and Path(marker['root']).resolve() == root
assert Path(marker['sourceRoot']).resolve() == source
sys.path.insert(0, str(root))
from backend.app.session_store import SessionStore

data = root / '.data/development'
sessions = SessionStore(data / 'sessions.db')
folder = data / 'roles' / role / 'memory'
assert folder.is_dir(), folder
if action == 'seed':
    assert not sessions.list_messages('role:' + role)
    sessions.open_role_session(role)
    for index in range(8):
        sessions.append_message('role:' + role, 'user', f'独立手动压缩历史 {index}')
        sessions.append_message('role:' + role, 'assistant', '我记得这件事')
    (folder / 'SELF.md').write_text('HOST_SELF_E2E 我是你的助手。', encoding='utf-8')
    (folder / 'MEMORY.md').write_text('HOST_MEMORY_E2E 你喜欢茶。', encoding='utf-8')
elif action == 'seed-stop':
    assert not sessions.list_messages('role:' + role)
    sessions.open_role_session(role)
    # Below the normal 20 + 10 message backlog gate, above the token gate.
    # Foreground coverage owns this draft; it is not waiting on background work.
    for index in range(12):
        sessions.append_message('role:' + role, 'user', f'旧问题{index}：' + '历史事实。' * 500)
        sessions.append_message('role:' + role, 'assistant', '原有答复。' * 500)
elif action == 'optimizer-seed':
    (folder / 'MEMORY.md').write_text('OPTIMIZER_ORIGINAL_MEMORY', encoding='utf-8')
    (folder / 'SELF.md').write_text('OPTIMIZER_ORIGINAL_SELF', encoding='utf-8')
    (folder / 'PENDING.md').write_text('# 待整理的记忆\n- CANDIDATE_INITIAL\n', encoding='utf-8')
    (folder / 'PENDING.snapshot.md').write_text('# 待整理的记忆\n- CANDIDATE_RECOVERED\n', encoding='utf-8')
elif action == 'optimizer-append':
    with (folder / 'PENDING.md').open('a', encoding='utf-8') as stream:
        stream.write('- CANDIDATE_CONCURRENT\n')
with sqlite3.connect(data / 'sessions.db') as db:
    db.row_factory = sqlite3.Row
    windows = ([dict(row) for row in db.execute('SELECT * FROM context_windows WHERE role_id=?', (role,))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='context_windows'").fetchone() else [])
with sqlite3.connect(data / 'memory2.db') as db:
    records = db.execute('SELECT count(*) FROM memory_items WHERE role_id=?', (role,)).fetchone()[0]
print(json.dumps({'progress': sessions.maintenance_progress(role, 'role:' + role),
    'windows': windows, 'messages': len(sessions.list_messages('role:' + role)), 'records': records,
    'snapshot': (folder / 'PENDING.snapshot.md').exists(),
    'documents': {name: (folder / name).read_text(encoding='utf-8') if (folder / name).exists() else ''
        for name in ('SELF.md', 'MEMORY.md', 'HISTORY.md', 'PENDING.md', 'RECENT_CONTEXT.md')}}, ensure_ascii=False))
