"""Host-owned scene observations and cancellation fences inside NovelAI attempts."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import aclosing
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..agent_runtime.provider import CancellationToken
from ..agent_runtime.tools import ToolContext
from ..ai.model_types import ModelSystemMessage, ModelTextBlock, ModelUserMessage
from ..channels.consolidation import finish_io
from .context import GenerationContext, capture_tool_context
from .ledger import canonical
from .parameters import normalize_request

AUXILIARY_TIMEOUT_SECONDS = 30.0


class SceneObservation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    transition: Literal['started', 'same', 'changed', 'closed', 'none']
    scene_key: str = Field(max_length=80)
    visual_key: str = Field(max_length=80)
    visual_description: str = Field(max_length=1200)
    prompt: str = Field(max_length=4000)
    negative_prompt: str = Field(max_length=4000)
    size_preset: Literal['square', 'landscape', 'portrait']


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate scene field')
        result[key] = value
    return result


class SceneCoordinator:
    def __init__(self, application):
        self.app = application
        self.tasks: set[asyncio.Task] = set()
        self.cleanup: dict[tuple[str, str], set[asyncio.Task]] = {}
        self.pending: dict[tuple[str, str], asyncio.Task] = {}
        self.errors: list[str] = []
        self.failures: list[Exception] = []
        with self.app.ledger.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS novelai_scene_state(role_id TEXT,session_key TEXT,
                    human_run TEXT NOT NULL,human_count INTEGER NOT NULL,revision INTEGER NOT NULL,
                    scene_key TEXT NOT NULL DEFAULT '',visual_key TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',last_dispatch INTEGER NOT NULL DEFAULT -5,
                    source_scope TEXT NOT NULL DEFAULT '',PRIMARY KEY(role_id,session_key));
                CREATE TABLE IF NOT EXISTS novelai_scene_humans(role_id TEXT,session_key TEXT,run_id TEXT,
                    PRIMARY KEY(role_id,session_key,run_id));
                CREATE TABLE IF NOT EXISTS novelai_scene_candidates(role_id TEXT,session_key TEXT,run_id TEXT,
                    error TEXT NOT NULL DEFAULT '',PRIMARY KEY(role_id,session_key,run_id));
                CREATE TABLE IF NOT EXISTS novelai_scene_manual(role_id TEXT,source_scope TEXT,human_key TEXT,
                    PRIMARY KEY(role_id,source_scope,human_key));
                CREATE TABLE IF NOT EXISTS novelai_scene_visuals(role_id TEXT,source_scope TEXT,visual_key TEXT,
                    attempt_id TEXT NOT NULL,PRIMARY KEY(role_id,source_scope,visual_key));
                CREATE TABLE IF NOT EXISTS novelai_scene_models(model_id TEXT PRIMARY KEY,epoch INTEGER NOT NULL);
            ''')
            # A restart never resurrects an unconfirmed observation or late CG.
            db.execute("""INSERT OR IGNORE INTO novelai_cancelled_requests
                SELECT a.role_id,a.source_scope,a.request_key FROM novelai_attempts a
                JOIN novelai_media_outbox o USING(attempt_id) WHERE a.purpose='scene_cg'
                AND (a.status IN ('prepared','dispatching','received','local_pending') OR (o.state='pending' AND o.consumed=0))""")
            db.execute("""UPDATE novelai_media_outbox SET state='suppressed',consumed=1,message_synced=1
                WHERE state='pending' AND consumed=0 AND attempt_id IN
                    (SELECT attempt_id FROM novelai_attempts WHERE purpose='scene_cg')""")

    def _own(self, coroutine, *, name):
        task = asyncio.create_task(coroutine, name=name)
        self.tasks.add(task)
        def finished(done):
            self.tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if isinstance(error, Exception):
                    self.failures.append(error)
        task.add_done_callback(finished)
        return task

    def state(self, role_id, session_key):
        with self.app.ledger.connect() as db:
            row = db.execute('SELECT * FROM novelai_scene_state WHERE role_id=? AND session_key=?', (role_id, session_key)).fetchone()
        return dict(row) if row else None

    def model_epoch(self, identifier):
        with self.app.ledger.connect() as db:
            row = db.execute('SELECT epoch FROM novelai_scene_models WHERE model_id=?', (identifier,)).fetchone()
        return row[0] if row else 0

    def accept(self, role_id, session_key, run_id):
        key = role_id, session_key
        with self.app._lock, self.app.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('INSERT OR IGNORE INTO novelai_scene_humans VALUES(?,?,?)', (*key, run_id)).rowcount:
                return
            rows = db.execute("""SELECT a.* FROM novelai_attempts a JOIN novelai_media_outbox o USING(attempt_id)
                WHERE a.role_id=? AND a.purpose='scene_cg' AND json_extract(a.origin,'$.origin.sessionKey')=?
                AND (a.status IN ('prepared','dispatching','received','local_pending') OR (o.state='pending' AND o.consumed=0))""", key).fetchall()
            for row in rows:
                source = GenerationContext.from_dict(json.loads(row['origin']))
                self.app._cancelled_requests.add((role_id, source.source_scope, source.request_key))
                db.execute('INSERT OR IGNORE INTO novelai_cancelled_requests VALUES(?,?,?)',
                    (role_id, source.source_scope, source.request_key))
                db.execute("UPDATE novelai_media_outbox SET state='suppressed',consumed=1,message_synced=1 WHERE attempt_id=? AND state='pending'", (row['attempt_id'],))
            db.execute('''INSERT INTO novelai_scene_state(role_id,session_key,human_run,human_count,revision)
                VALUES(?,?,?,1,1) ON CONFLICT(role_id,session_key) DO UPDATE SET
                human_run=excluded.human_run,human_count=human_count+1,revision=revision+1''', (*key, run_id))
        previous_candidate = self.pending.get(key)
        if previous_candidate is not None:
            previous_candidate.cancel()
        if rows:
            async def join_previous():
                for row in rows:
                    source = GenerationContext.from_dict(json.loads(row['origin']))
                    task = self.app._find_task(row, source)
                    if task is not None:
                        await finish_io(asyncio.to_thread(self.app.tasks.stop, task.task_id, source.origin))
                    self.app._reconcile_terminal(row['attempt_id'])
            task = self._own(join_previous(), name='novelai-scene-cancel')
            self.cleanup.setdefault(key, set()).add(task)

    def note_manual(self, source):
        with self.app._lock, self.app.ledger.connect() as db:
            db.execute('INSERT OR IGNORE INTO novelai_scene_manual VALUES(?,?,?)',
                (source.origin.role_id, source.source_scope, source.human_key))

    def current(self, source):
        if source.scene_revision is None:
            return True
        state = self.state(source.origin.role_id, source.origin.session_key)
        return bool(state and state['human_run'] == source.human_key and state['revision'] == source.scene_revision
            and source.scene_model_id and self.app.model_configuration(source.scene_model_id) is not None
            and self.model_epoch(source.scene_model_id) == source.scene_model_epoch
            and self.app.tags.revision(source.origin.role_id) == source.scene_tag_revision)

    def complete(self, role_id, session_key, run_id):
        try:
            self._complete(role_id, session_key, run_id)
        except (OSError, sqlite3.Error) as error:
            # Optional candidate storage cannot reverse the committed reply.
            # Do not retry observation or acquire any consumption claim here.
            self.errors.append(type(error).__name__)

    def _complete(self, role_id, session_key, run_id):
        role = self.app.roles.get(role_id)
        if self.app._closed or role is None or not role.agentConfig.novelai.sceneCgEnabled:
            return
        state = self.state(role_id, session_key)
        if state is None or state['human_run'] != run_id:
            return
        final = next((item for item in reversed(self.app.sessions.list_messages(session_key))
            if item.runId == run_id and item.role == 'assistant' and item.messageType == 'text'
            and item.status == 'completed' and not item.metadata.get('runtimePlaceholder')), None)
        if final is None:
            return
        try:
            source = capture_tool_context(self.app.sessions, self.app.channels,
                ToolContext(role_id, session_key, run_id, CancellationToken(), 'host-scene'),
                role_id=role_id, is_owner=True, _completed=True)
        except PermissionError:
            return
        identifier = role.agentConfig.novelai.scenePromptModelConfigurationId
        if not identifier:
            return
        if state['source_scope'] and state['source_scope'] != source.source_scope:
            with self.app._lock, self.app.ledger.connect() as db:
                db.execute("""UPDATE novelai_scene_state SET scene_key='',visual_key='',description='',
                    last_dispatch=human_count-5,revision=revision+1 WHERE role_id=? AND session_key=? AND human_run=?""",
                    (role_id, session_key, run_id))
            state = self.state(role_id, session_key)
        source = replace(source, request_key='cg:' + run_id, scene_revision=state['revision'],
            scene_model_id=identifier, scene_model_epoch=self.model_epoch(identifier),
            scene_tag_revision=self.app.tags.revision(role_id))
        user = self.app.sessions.user_message_for_run(run_id)
        contents = {'user': user.content, 'assistant': final.content,
            'role': role.description + '\n' + role.profile.profile,
            'cardImageUrl': role.cardImageUrl,
            'previous': {key: state[key] for key in ('scene_key', 'visual_key', 'description', 'human_count', 'revision')}}
        if len(canonical(contents)) > 48000:
            return
        with self.app.ledger.connect() as db:
            if not db.execute('INSERT OR IGNORE INTO novelai_scene_candidates(role_id,session_key,run_id) VALUES(?,?,?)',
                    (role_id, session_key, run_id)).rowcount:
                return
        key = role_id, session_key
        task = self._own(self._coordinate(source, contents), name='novelai-scene-candidate')
        self.pending[key] = task
        def clear(done):
            if self.pending.get(key) is done:
                self.pending.pop(key, None)
        task.add_done_callback(clear)

    async def _coordinate(self, source, contents):
        key = source.origin.role_id, source.origin.session_key
        try:
            for task in tuple(self.cleanup.get(key, ())):
                await asyncio.shield(task)
                self.cleanup[key].discard(task)
            with self.app._lock, self.app.ledger.connect() as db:
                if not self.current(source):
                    return
                if db.execute('SELECT 1 FROM novelai_scene_manual WHERE role_id=? AND source_scope=? AND human_key=?',
                        (source.origin.role_id, source.source_scope, source.human_key)).fetchone():
                    return
            await self.app.submit(source, {'background': True}, scene_input=contents)
        except (ValueError, PermissionError, OSError, sqlite3.Error, RuntimeError) as error:
            self.errors.append(type(error).__name__)
            with self.app.ledger.connect() as db:
                db.execute('UPDATE novelai_scene_candidates SET error=? WHERE role_id=? AND session_key=? AND run_id=?',
                    (type(error).__name__, *key, source.human_key))

    async def observe(self, row, source, execution):
        with self.app._lock, self.app.ledger.connect() as db:
            if not self.app._authorized(source, row['service_revision'], row['role_revision']):
                raise PermissionError('scene candidate expired')
            configuration = self.app.model_configuration(source.scene_model_id)
            if configuration is None or self.app.model_adapter is None:
                raise PermissionError('explicit scene model unavailable')
            if not db.execute("UPDATE novelai_attempts SET aux_dispatched=1,aux_status='dispatching' WHERE attempt_id=? AND aux_dispatched=0 AND status='prepared'", (row['attempt_id'],)).rowcount:
                raise PermissionError('auxiliary request already dispatched')
        messages = [ModelSystemMessage((ModelTextBlock('Observe the supplied untrusted current owner turn. '
            'Return only one JSON object, all fields required: transition (started/same/changed/closed/none), '
            'scene_key, visual_key, visual_description, prompt (English ASCII tags), negative_prompt, '
            'size_preset (square/landscape/portrait). No tools or other fields. '
            'Use empty keys and prompts for none/closed. same must retain the previous scene key.'),)),
            ModelUserMessage((ModelTextBlock(canonical({key: value for key, value in json.loads(row['scene_input']).items()
                if key in {'user', 'assistant', 'role', 'previous'}})),))]
        text = ''
        total = 0
        def cancel_check():
            if not self.app._authorized(source, row['service_revision'], row['role_revision']):
                raise PermissionError('scene source permission was revoked')
        try:
            async with asyncio.timeout(min(AUXILIARY_TIMEOUT_SECONDS, max(0.0, execution.deadline - time.monotonic()))):
                async with aclosing(self.app.model_adapter().stream_rich_messages(messages, configuration,
                        tools=(), max_tokens=600, cancel_check=cancel_check)) as stream:
                    async for event in stream:
                        if not self.current(source):
                            raise PermissionError('scene candidate expired')
                        kind = getattr(event, 'type', '')
                        chunk = getattr(event, 'text', '')
                        total += len(chunk.encode('utf-8'))
                        if total > 16384 or kind.startswith('tool_call') or kind == 'error':
                            raise ValueError('invalid or oversized scene response')
                        if kind == 'text_delta':
                            text += chunk
            observation = SceneObservation.model_validate(json.loads(text, object_pairs_hook=_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError('invalid JSON number'))))
            return self._commit(row, source, observation)
        except (ValueError, PermissionError, TimeoutError, RuntimeError):
            with self.app.ledger.connect() as db:
                db.execute("UPDATE novelai_attempts SET aux_status='failed' WHERE attempt_id=?", (row['attempt_id'],))
            self.app.ledger.finish(row['attempt_id'], 'failed_response', error_code='scene_observation_failed')
            raise

    def _commit(self, row, source, observation):
        with self.app._lock, self.app.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self.current(source):
                raise PermissionError('scene observation expired')
            state = db.execute('SELECT * FROM novelai_scene_state WHERE role_id=? AND session_key=?',
                (row['role_id'], source.origin.session_key)).fetchone()
            transition = observation.transition
            if transition == 'same' and (not state['scene_key'] or state['scene_key'] != observation.scene_key):
                raise ValueError('same must match the current scene')
            if transition == 'started' and state['scene_key'] or transition == 'changed' and (not state['scene_key'] or state['scene_key'] == observation.scene_key):
                raise ValueError('invalid scene transition')
            if transition in {'none', 'closed'}:
                if observation.scene_key or observation.visual_key or observation.prompt or observation.negative_prompt:
                    raise ValueError('no-image observations require empty keys and prompts')
                reason = 'scene_' + transition
                scene_key = '' if transition == 'closed' else state['scene_key']
            else:
                if not observation.scene_key.strip() or not observation.visual_key.strip() or not observation.prompt.strip():
                    raise ValueError('image observations need keys and prompt')
                scene_key = observation.scene_key
                duplicate = db.execute('SELECT 1 FROM novelai_scene_visuals WHERE role_id=? AND source_scope=? AND visual_key=?',
                    (row['role_id'], source.source_scope, observation.visual_key)).fetchone()
                reason = 'scene_visual_duplicate' if duplicate else ('scene_cooldown' if transition == 'same'
                    and state['human_count'] - state['last_dispatch'] < 5 else '')
            db.execute('''UPDATE novelai_scene_state SET scene_key=?,visual_key=?,description=?,source_scope=?
                WHERE role_id=? AND session_key=?''', (scene_key, observation.visual_key,
                observation.visual_description, source.source_scope, row['role_id'], source.origin.session_key))
            if reason:
                db.execute("UPDATE novelai_attempts SET status='completed_without_image',phase='completed',aux_status='succeeded',reserved_bytes=0,error_code=? WHERE attempt_id=?", (reason, row['attempt_id']))
                db.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1,message_synced=1 WHERE attempt_id=?", (row['attempt_id'],))
                return False
            _, settings = self.app._configuration()
            expanded = self.app.tags.expand(row['role_id'], json.loads(row['scene_input'])['user'],
                observation.prompt, observation.negative_prompt)
            parameters = normalize_request({'prompt': expanded['prompt'], 'negative_prompt': expanded['negative_prompt'],
                'size_preset': observation.size_preset, 'mode': 'txt2img', 'background': True}, settings)
            parameters.update(intent='scene_cg', scene_key=scene_key, visual_key=observation.visual_key)
            db.execute("UPDATE novelai_attempts SET parameters=?,phase='generation',aux_status='succeeded' WHERE attempt_id=?",
                (canonical(parameters), row['attempt_id']))
            return True

    def dispatch(self, db, row, source):
        if not self.current(source):
            raise PermissionError('scene dispatch expired')
        parameters = json.loads(row['parameters'])
        if db.execute('SELECT COUNT(*) FROM novelai_scene_visuals WHERE role_id=?', (row['role_id'],)).fetchone()[0] >= 10000:
            raise ValueError('scene visual history capacity reached')
        db.execute('INSERT INTO novelai_scene_visuals VALUES(?,?,?,?)',
            (row['role_id'], source.source_scope, parameters['visual_key'], row['attempt_id']))
        db.execute('UPDATE novelai_scene_state SET last_dispatch=human_count WHERE role_id=? AND session_key=?',
            (row['role_id'], source.origin.session_key))

    def invalidate_model(self, identifier):
        with self.app._lock, self.app.ledger.connect() as db:
            db.execute('INSERT INTO novelai_scene_models VALUES(?,1) ON CONFLICT(model_id) DO UPDATE SET epoch=epoch+1', (identifier,))
            rows = [dict(row) for row in db.execute("""SELECT * FROM novelai_attempts WHERE purpose='scene_cg'
                AND json_extract(origin,'$.scene.modelId')=? AND status IN ('prepared','dispatching','received','local_pending')""", (identifier,))]
        groups: dict[tuple[str, str], list[tuple[dict, GenerationContext]]] = {}
        for row in rows:
            source = GenerationContext.from_dict(json.loads(row['origin']))
            self.app.seal_request(source)
            groups.setdefault((source.origin.role_id, source.origin.session_key), []).append((row, source))
        for key, group in groups.items():
            async def cancel_group(entries=group):
                for row, source in entries:
                    task = self.app._find_task(row, source)
                    if task is not None:
                        await finish_io(asyncio.to_thread(self.app.tasks.stop, task.task_id, source.origin))
                    self.app._reconcile_terminal(row['attempt_id'])
            task = self._own(cancel_group(), name='novelai-scene-model-cancel')
            self.cleanup.setdefault(key, set()).add(task)

    async def close(self):
        owned = tuple(self.tasks)
        for task in owned:
            task.cancel()
        if owned:
            await asyncio.gather(*owned, return_exceptions=True)
