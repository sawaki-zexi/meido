"""Owner-authored channel facts use host documents without a private recent view.

Listening and addressed group conversations have independent durable progress;
semantic publication is an acknowledged, retryable consumer of host commits.
"""
from __future__ import annotations

import asyncio

from .memory_document_lock import document_lock
from .memory_documents import MemoryDocuments
from .memory_events import ConsolidationCommitted
from .memory_maintenance import MemoryMaintenance
from .memory_optimizer import MemoryOptimizer


class OwnerChannelMemory:
    def __init__(self, sessions, roles_root, extract, publish, verify, *, semantic_lock=None):
        self.sessions, self.roles_root = sessions, roles_root
        self.extract, self.publish, self.verify = extract, publish, verify
        self.tasks = {}
        self.errors = []
        self._semantic_locks = {}
        self.semantic_lock = semantic_lock or (lambda role_id: self._semantic_locks.setdefault(role_id, asyncio.Lock()))

    async def commit(self, thread, messages, *, listening=True):
        if not messages:
            return
        key = ('listening:' if listening else 'group-owner:') + thread.thread_id
        ids = [str(item['id']) for item in messages]
        proof = await asyncio.to_thread(self.verify, thread.role_id, key, ids)
        if not proof:
            raise PermissionError('主人发言来源许可已变化')
        source = f'{key}:{messages[0]["sequence"]}-{messages[-1]["sequence"]}:{proof[:12]}'
        previous = await asyncio.to_thread(self.sessions.maintenance_progress, thread.role_id, key)
        if previous.get('lastSourceKey') != source:
            draft = await asyncio.to_thread(self.extract, thread.role_id, messages)
            event = ConsolidationCommitted(thread.role_id, thread.session_key, source, tuple(ids),
                (messages[0]['sequence'], messages[-1]['sequence']), (),
                history_entries=tuple(draft['history_entries']), conversation=draft['conversation'])
            documents = MemoryDocuments(self.roles_root)
            folder = documents.memory_dir(thread.role_id)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'journal').mkdir(exist_ok=True)
            with document_lock(folder):
                if self.verify(thread.role_id, key, ids) != proof:
                    raise PermissionError('主人发言在整理期间已变化')
                current = self.sessions.maintenance_progress(thread.role_id, key)
                if current.get('lastSourceKey') != previous.get('lastSourceKey'):
                    raise ValueError('主人发言维护进度已变化')
                marker = f'\n<!-- source: {source} -->\n'
                writes = {}
                for name, body in (('HISTORY.md', draft['history']), ('PENDING.md', draft['pending'])):
                    path = folder / name
                    old = MemoryOptimizer._read_text(path)
                    if source not in old:
                        writes[path] = old.rstrip() + '\n\n' + body + marker
                journal = documents.journal_path(thread.role_id, str(messages[0]['createdAt'])[:10])
                old = MemoryOptimizer._read_text(journal)
                if source not in old:
                    writes[journal] = old + documents.render_journal_entry(source, messages[0]['sequence'], messages[-1]['sequence'])
                progress = {**current, 'lastSequence': messages[-1]['sequence'], 'lastSourceKey': source,
                    'pendingEvents': [*current.get('pendingEvents', []), {'proof': proof,
                        'payload': MemoryMaintenance._event_payload(event)}]}
                self.sessions.save_maintenance_progress(thread.role_id, key, progress, expected=current,
                    write_documents=lambda: MemoryOptimizer.commit_documents(writes))
        self.schedule(thread.role_id, key)

    def schedule(self, role_id, key):
        if (role_id, key) in self.tasks:
            return
        task = asyncio.create_task(self.retry(role_id, key))
        self.tasks[role_id, key] = task
        task.add_done_callback(lambda done: self.tasks.pop((role_id, key), None))

    def resume(self, role_id):
        for key, progress in self.sessions.maintenance_progress_entries(role_id):
            if key.startswith(('listening:', 'group-owner:')) and progress.get('pendingEvents'):
                self.schedule(role_id, key)

    async def retry(self, role_id, key):
        try:
            from .channels.consolidation import finish_io
            current = await asyncio.to_thread(self.sessions.maintenance_progress, role_id, key)
            for entry in tuple(current.get('pendingEvents', [])):
                async with self.semantic_lock(role_id):
                    await finish_io(asyncio.to_thread(self._publish_entry, role_id, key, entry))
        except Exception as error:
            self.errors.append(f'{role_id}: semantic publication pending: {error}')

    def _publish_entry(self, role_id, key, entry):
        current = self.sessions.maintenance_progress(role_id, key)
        if entry not in current.get('pendingEvents', []):
            return
        event = MemoryMaintenance._event_from_payload(entry['payload'])
        proof = self.verify(role_id, key, list(event.message_ids))
        if proof == entry['proof']:
            self.publish(event)
        # Recheck the source after waiting for the semantic lane. Revoked
        # payloads remain auditable, and can never acquire a publication ACK.
        folder = MemoryDocuments(self.roles_root).memory_dir(role_id)
        with document_lock(folder):
            current = self.sessions.maintenance_progress(role_id, key)
            progress = {**current, 'pendingEvents': [item for item in current.get('pendingEvents', []) if item != entry]}
            if proof != entry['proof']:
                progress['invalidatedEvents'] = [*current.get('invalidatedEvents', []), entry]
            self.sessions.save_maintenance_progress(role_id, key, progress, expected=current)

    async def drain(self, role_id=None):
        tasks = [task for (role, _), task in self.tasks.items() if role_id is None or role == role_id]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
