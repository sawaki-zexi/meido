"""Own the paid-attempt boundary from real human intent to original-source media."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from ..channels.consolidation import finish_io
from ..chat_attachments import resolve_attachment
from ..task_runtime import TaskOrigin
from ..task_runtime.runtime import TaskBudget, TaskOutcome
from .config import NovelaiRoleConfig, NovelaiSettings, NovelaiSettingsStore
from .context import GenerationContext
from .executor import GenerationExecutor
from .http import GenerationHttpError, NovelaiHttp
from .ledger import GenerationLedger, canonical
from .media import import_response, owned_outputs, validate_image
from .parameters import generation_payload, normalize_request


@dataclass(frozen=True)
class GenerationReply:
    attempt_id: str
    role_id: str
    attachment_ids: tuple[str, ...]
    authorized: Callable[[], bool]


class NovelaiApplication:
    def __init__(self, *, root: Path, tasks, sessions, roles, channels, credentials,
                 is_role_deleting=lambda role_id: False, http=None, send_channel=None,
                 channel_media_supported=None):
        self.root, self.tasks, self.sessions, self.roles, self.channels = root, tasks, sessions, roles, channels
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.settings = NovelaiSettingsStore(sessions.database_path, credentials, lock=self._lock)
        self.ledger = GenerationLedger(sessions.database_path)
        self.http = http or NovelaiHttp()
        self.is_role_deleting, self.send_channel = is_role_deleting, send_channel
        self.channel_media_supported = channel_media_supported
        self._closed = False
        self._maintenance = None
        self._cancelled_requests: set[tuple[str, str, str]] = set()

    def _validate_policy(self, policy):
        if policy.sceneCgEnabled or policy.autoWritebackRoleAssets:
            raise ValueError('场景 CG 与资产写回尚未开放，请保持关闭')
        if policy.manualEnabled:
            public = self.settings.public()
            if not public['enabled'] or not public['tokenConfigured'] or public['credentialCleanupPending'] or policy.consentRevision != public['revision']:
                raise ValueError('请先配置服务并确认当前版本的有限生图消费许可')

    def create_role(self, data):
        with self._lock:
            self._validate_policy(data.agentConfig.novelai)
            return self.roles.create(data)

    def update_role(self, role_id, data):
        with self._lock:
            current = self.roles.get(role_id)
            if current is not None and data.agentConfig is not None and 'novelai' in data.agentConfig.model_fields_set:
                policy = current.agentConfig.novelai.model_copy(update=data.agentConfig.novelai.model_dump(exclude_unset=True))
                self._validate_policy(policy)
                if policy != current.agentConfig.novelai:
                    # Commit revocation before the independently atomic role
                    # file write. A file failure can only revoke conservatively.
                    with self.ledger.connect() as connection:
                        connection.execute('INSERT INTO novelai_role_epochs VALUES(?,1) ON CONFLICT(role_id) DO UPDATE SET revision=revision+1', (role_id,))
            return self.roles.update(role_id, data)

    def start(self):
        if self._closed:
            raise RuntimeError('generation application is closed')
        if self._maintenance is None:
            self._maintenance = asyncio.create_task(self._maintain(), name='novelai-local-recovery')

    async def _maintain(self):
        while not self._closed:
            try:
                await self.recover()
            except (OSError, sqlite3.Error):
                pass  # Owned records retain every repair obligation.
            await asyncio.sleep(.25)

    def _configuration(self):
        public = self.settings.public()
        return public, NovelaiSettings.model_validate({key: public[key] for key in NovelaiSettings.model_fields})

    def _role_revision(self, role):
        with self.ledger.connect() as connection:
            row = connection.execute('SELECT revision FROM novelai_role_epochs WHERE role_id=?', (role.id,)).fetchone()
        return hashlib.sha256(canonical([role.createdAt.isoformat(), row[0] if row else 0, role.agentConfig.novelai.model_dump()]).encode()).hexdigest()

    def unavailable_reason(self, role, *, include_usage=True):
        config, _ = self._configuration()
        if self._closed or role is None or self.is_role_deleting(role.id):
            return '角色生图服务当前不可用'
        if not config['enabled']:
            return 'NovelAI 服务已关闭'
        if not config['tokenConfigured'] or config['credentialCleanupPending']:
            return '尚未配置独立 NovelAI 凭据，或凭据清理尚未完成'
        policy = role.agentConfig.novelai
        if not policy.manualEnabled:
            return '角色的手动生图许可已关闭'
        if policy.consentRevision != config['revision']:
            return '请主人确认当前服务设置的有限生图消费许可'
        usage = self.ledger.usage(role.id)
        if include_usage and (usage['accountAttempts'] >= config['dailyAttemptLimit'] or usage['manualAttempts'] >= policy.manualDailyAttempts):
            return '今日生图尝试额度已用完；次数不是 Anlas 估价'
        return ''

    def _authorized(self, source: GenerationContext, service_revision, role_revision, *, allow_sealed=False):
        role = self.roles.get(source.origin.role_id)
        public = self.settings.public()
        return bool(not self._closed and (allow_sealed or not self._sealed(source)) and role is not None and not self.is_role_deleting(role.id)
            and public['enabled'] and public['tokenConfigured'] and not public['credentialCleanupPending']
            and public['revision'] == service_revision and role.agentConfig.novelai.manualEnabled
            and role.agentConfig.novelai.consentRevision == public['revision']
            and self._role_revision(role) == role_revision and source.denial(self.channels) is None)

    def _seal_path(self, source):
        identifier = hashlib.sha256(canonical([source.origin.role_id, source.source_scope, source.request_key]).encode()).hexdigest()
        return self.root / source.origin.role_id / (identifier + '.cancelled')

    def _sealed(self, source):
        return ((source.origin.role_id, source.source_scope, source.request_key) in self._cancelled_requests
            or self._seal_path(source).exists()
            or self.ledger.sealed(source.origin.role_id, source.source_scope, source.request_key))

    def seal_request(self, source):
        """Persist loss of publication authority before asking a worker to stop.

        The small owned marker also survives a busy/unavailable business database;
        local recovery can project it into the canonical outbox without sending.
        """
        with self._lock:
            self._cancelled_requests.add((source.origin.role_id, source.source_scope, source.request_key))
            marker = self._seal_path(source)
            marker_failure = None
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                with marker.open('xb') as handle:
                    handle.write(b'cancelled\n')
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                pass
            except OSError as error:
                marker_failure = error
            try:
                self.ledger.seal(source.origin.role_id, source.source_scope, source.request_key)
            except sqlite3.Error:
                if marker_failure is not None:
                    raise marker_failure

    def _input(self, source: GenerationContext, request):
        if request['mode'] != 'img2img':
            return None
        identifier = request['base_attachment_id']
        if identifier not in source.attachment_ids:
            raise PermissionError('img2img input is not an allowed current-source attachment')
        item = resolve_attachment(self.roles.workspace_path(source.origin.role_id), identifier)
        if item is None or item.get('kind') != 'image':
            raise PermissionError('img2img input is unavailable in this role')
        input_path = item.get('path')
        if not isinstance(input_path, str):
            raise PermissionError('img2img input has no owned file')
        path = Path(input_path)
        with path.open('rb') as handle:
            content = handle.read(10 * 1024 * 1024 + 1)
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError('img2img input exceeds 10 MiB')
        validate_image(content, max_pixels=16 * 1024 * 1024)
        return content

    def _find_task(self, row, source):
        if row['task_id']:
            try:
                return self.tasks.query(row['task_id'], source.origin)
            except KeyError:
                return None
        marker = 'NovelAI attempt:' + row['attempt_id']
        return next((item for item in self.tasks.list(source.origin, kind='novelai_image') if item.description == marker), None)

    async def submit(self, source: GenerationContext, arguments: dict[str, object], *, signal=None, expected_revision=None, expected_role_revision=None):
        handover_error = None
        with self._lock:
            role = self.roles.get(source.origin.role_id)
            reason = self.unavailable_reason(role, include_usage=False)
            if reason:
                raise PermissionError(reason)
            if expected_role_revision is not None and self._role_revision(role) != expected_role_revision:
                raise PermissionError('frozen generation role permission changed')
            if source.denial(self.channels):
                raise PermissionError('original generation source was revoked')
            if source.channel_source is not None and (self.channel_media_supported is None
                    or not self.channel_media_supported(source.origin.source_account_id)):
                raise PermissionError('original channel cannot automatically deliver image media')
            public, settings = self._configuration()
            if expected_revision is not None and public['revision'] != expected_revision:
                raise ValueError('generation service revision changed')
            fingerprint = hashlib.sha256(canonical(arguments).encode()).hexdigest()
            row = self.ledger.request(role.id, source.source_scope, source.request_key)
            existing = row is not None
            owned_input = None
            if existing:
                if row['fingerprint'] != fingerprint or row['origin'] != canonical(source.to_dict()):
                    raise ValueError('request parameters conflict with saved request')
            else:
                if self._sealed(source):
                    raise PermissionError('generation request was cancelled')
                parameters = normalize_request(arguments, settings)
                owned_input = self._input(source, parameters)
                if owned_input is not None:
                    parameters['inputSha256'] = hashlib.sha256(owned_input).hexdigest()
                view = self.ledger.prepare(role_id=role.id, source_scope=source.source_scope,
                    human_key=source.human_key, request_key=source.request_key, parameters=parameters,
                    origin=source.to_dict(), service_revision=public['revision'], role_revision=self._role_revision(role),
                    account_limit=settings.dailyAttemptLimit, role_limit=role.agentConfig.novelai.manualDailyAttempts,
                    fingerprint=fingerprint)
                row = self.ledger.attempt(view['attemptId'])
            task = self._find_task(row, source)
            if existing and task is None and row['status'] == 'prepared':
                self.ledger.finish(row['attempt_id'], 'cancelled_before_dispatch')
            elif task is None and row['status'] == 'prepared':
                admitted_task_id = None
                try:
                    task_id = self.tasks.submit(source.origin,
                        GenerationExecutor(lambda execution: self._execute(row, source, owned_input, execution)),
                        budget=TaskBudget(300, 4000, 64 * 1024), kind='novelai_image',
                        description='NovelAI attempt:' + row['attempt_id'],
                        authorized=lambda: self._authorized(source, row['service_revision'], row['role_revision']),
                        completion=False, submission_key=row['attempt_id'], retain_until_ack=True)
                    admitted_task_id = task_id
                    self.ledger.set_task(row['attempt_id'], task_id)
                    task = self.tasks.query(task_id, source.origin)
                except (OSError, sqlite3.Error) as error:
                    if admitted_task_id is None:
                        self.ledger.finish(row['attempt_id'], 'rejected', error_code='task_admission_failed')
                        raise
                    # Runtime has accepted ownership. A failed business copy is
                    # never permission to detach a foreground paid operation.
                    task = self.tasks.query(admitted_task_id, source.origin)
                    try:
                        self.seal_request(source)
                    except OSError:
                        pass  # In-memory seal still denies dispatch while join owns it.
                    handover_error = error
                except (ValueError, PermissionError):
                    self.ledger.finish(row['attempt_id'], 'rejected', error_code='task_admission_rejected')
                    raise
        if handover_error is not None:
            await finish_io(asyncio.to_thread(self.tasks.stop, task.task_id, source.origin))
            self._reconcile_terminal(row['attempt_id'])
            raise handover_error
        if task is not None:
            try:
                while task.ended_at is None:
                    if signal is not None and signal.is_cancelled():
                        await finish_io(asyncio.to_thread(self.tasks.stop, task.task_id, source.origin))
                        break
                    await asyncio.sleep(.01)
                    task = self.tasks.query(task.task_id, source.origin)
            except asyncio.CancelledError:
                await finish_io(asyncio.to_thread(self.tasks.stop, task.task_id, source.origin))
                self._reconcile_terminal(row['attempt_id'])
                raise
            self._reconcile_terminal(row['attempt_id'])
            try:
                self.tasks.acknowledge_result(task.task_id, source.origin)
                with self.ledger.connect() as connection:
                    connection.execute('UPDATE novelai_attempts SET runtime_ack_pending=0 WHERE attempt_id=?', (row['attempt_id'],))
            except (sqlite3.Error, OSError, KeyError, ValueError, PermissionError):
                pass  # Durable ack_pending is repaired locally, never by re-launching.
        if not self._authorized(source, row['service_revision'], row['role_revision'], allow_sealed=existing):
            raise PermissionError('generation permission changed; result remains in owned history')
        return self.ledger.get(role.id, source.source_scope, row['generation_id'])

    async def _execute(self, row, source, owned_input, execution):
        spool = self.root / row['role_id'] / (row['attempt_id'] + '.response')
        try:
            with self._lock:
                if not self._authorized(source, row['service_revision'], row['role_revision']):
                    self.ledger.finish(row['attempt_id'], 'cancelled_before_dispatch')
                    return TaskOutcome('cancelled')
                token = self.settings.dispatch_token()
                payload = generation_payload(json.loads(row['parameters']), base_image=owned_input)
                self.ledger.mark_dispatching(row['attempt_id'])
            execution.progress('generation dispatched')
            received = await self.http.generate(payload, token, spool, deadline=execution.deadline)
            del payload, token, owned_input
            self.ledger.received(row['attempt_id'], received.media_type, received.size)
            execution.progress('response received; importing local images')
            self._import_received(self.ledger.attempt(row['attempt_id']))
            return TaskOutcome('succeeded', output_tail=canonical({'attemptId': row['attempt_id'], 'status': 'succeeded'}))
        except asyncio.CancelledError:
            self._reconcile_terminal(row['attempt_id'])
            raise
        except GenerationHttpError as error:
            self.ledger.finish(row['attempt_id'], 'failed_response' if error.definite_response else 'unknown', error_code=error.category)
            self._settle_failure(row)
            return TaskOutcome('failed')
        except (OSError, ValueError, PermissionError, sqlite3.Error):
            self._reconcile_terminal(row['attempt_id'])
            return TaskOutcome('failed')

    def _import_received(self, row):
        spool = self.root / row['role_id'] / (row['attempt_id'] + '.response')
        try:
            workspace = self.roles.workspace_path(row['role_id'])
            settings = json.loads(row['parameters'])
            max_pixels = settings['width'] * settings['height']
            if spool.exists():
                attachments = import_response(spool, row['media_type'], workspace, row['attempt_id'], max_pixels=max_pixels)
                self.ledger.staged_images(row['attempt_id'], attachments)
                spool.unlink()
            else:
                attachments = json.loads(row['attachments'])
                if not attachments:
                    raise OSError('received response and committed image references unavailable')
                for attachment in attachments:
                    item = resolve_attachment(workspace, attachment['id'])
                    if item is None or item['size'] != attachment['size'] or Path(item['path']).stat().st_size != attachment['size']:
                        raise OSError('previously imported image is unavailable')
                    validate_image(Path(item['path']).read_bytes(), max_pixels=max_pixels)
            self.ledger.imported(row['attempt_id'], attachments)
        except ValueError:
            self.ledger.finish(row['attempt_id'], 'unknown', error_code='response_not_verifiable')
            self._settle_failure(row)
            raise
        except (OSError, sqlite3.Error):
            self.ledger.finish(row['attempt_id'], 'local_pending', error_code='local_media_pending')
            raise

    def _settle_failure(self, row):
        parameters = json.loads(row['parameters'])
        attachments, size = owned_outputs(self.roles.workspace_path(row['role_id']), row['attempt_id'],
            max_pixels=parameters['width'] * parameters['height'])
        spool = self.root / row['role_id'] / (row['attempt_id'] + '.response')
        if spool.is_file() and not spool.is_symlink():
            size += spool.stat().st_size
        self.ledger.settled_failure(row['attempt_id'], attachments, size)

    def _reconcile_terminal(self, attempt_id):
        row = self.ledger.attempt(attempt_id)
        if row['status'] in {'prepared', 'dispatching'}:
            self.ledger.finish(attempt_id, 'unknown' if row['dispatched'] else 'cancelled_before_dispatch')
            self._settle_failure(row)
        elif row['status'] == 'received':
            self.ledger.finish(attempt_id, 'local_pending', error_code='local_media_pending')

    def history(self, role_id, *, limit=20, cursor=None):
        if self.roles.get(role_id) is None or self.is_role_deleting(role_id):
            raise KeyError('role unavailable')
        return {**self.ledger.list(role_id, limit=limit, cursor=cursor), 'usage': self.ledger.usage(role_id)}

    def owner_generation(self, role_id, generation_id):
        if self.roles.get(role_id) is None or self.is_role_deleting(role_id):
            raise KeyError('role unavailable')
        with self.ledger.connect() as connection:
            row = connection.execute('SELECT source_scope FROM novelai_generations WHERE generation_id=? AND role_id=?', (generation_id, role_id)).fetchone()
        if row is None:
            raise KeyError('generation unavailable')
        return self.ledger.get(role_id, row[0], generation_id)

    async def cancel(self, role_id, generation_id):
        view = self.owner_generation(role_id, generation_id)
        row = self.ledger.attempt(view['attemptId'])
        source = GenerationContext.from_dict(json.loads(row['origin']))
        self.seal_request(source)
        task = self._find_task(row, source)
        if task is not None and task.ended_at is None:
            await finish_io(asyncio.to_thread(self.tasks.stop, task.task_id, source.origin))
        self._reconcile_terminal(row['attempt_id'])
        return self.owner_generation(role_id, generation_id)

    async def attach_ui_completion(self, source: GenerationContext, attempt_id):
        if not source.ui_action or source.origin.source_account_id or source.origin.parent_run_id:
            raise PermissionError('generation completion requires an explicit owner UI action')
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT a.*,o.state AS outbox_state,o.consumed FROM novelai_attempts a JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id WHERE a.attempt_id=?', (attempt_id,)).fetchone()
            if row is None or row['status'] != 'succeeded' or row['origin'] != canonical(source.to_dict()):
                raise PermissionError('generation completion does not match original UI action')
            if row['outbox_state'] not in {'pending', 'sent'}:
                return None
            if row['error_code']:
                connection.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1 WHERE attempt_id=? AND state='pending'", (attempt_id,))
                return None
            authorize = lambda: self._authorized(source, row['service_revision'], row['role_revision'])
            message = self.sessions.append_generation_completion_once(row['role_id'], source.origin.session_key,
                attempt_id, json.loads(row['attachments']), connection=connection, authorized=authorize)
            connection.execute("UPDATE novelai_media_outbox SET state='sent',message_id=?,consumed=1,message_synced=1 WHERE attempt_id=?", (message.id, attempt_id))
            return message

    async def attach_foreground_reply(self, role_id, session_key, run_id, message_id):
        """Consume the one canonical outbox in the same transaction as message metadata."""
        if session_key.startswith('thread:'):
            raise ValueError('channel media must record its real delivery receipt')
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('''SELECT a.*,o.state AS outbox_state,o.message_id AS outbox_message,o.consumed
                FROM novelai_human_claims c JOIN novelai_attempts a ON a.attempt_id=c.attempt_id
                JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id
                WHERE c.role_id=? AND c.source_scope='desktop' AND c.human_key=?''', (role_id, run_id)).fetchall()
            updated = None
            for row in rows:
                if row['status'] != 'succeeded' or row['outbox_state'] not in {'pending', 'sent'}:
                    continue
                if row['error_code'] or len(json.loads(row['attachments'])) > 4:
                    connection.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1 WHERE attempt_id=? AND state='pending'", (row['attempt_id'],))
                    continue
                source = GenerationContext.from_dict(json.loads(row['origin']))
                if source.origin.session_key != session_key or source.origin.parent_run_id != run_id:
                    raise PermissionError('generation reply origin mismatch')
                if row['consumed'] and row['outbox_message'] != message_id:
                    continue
                authorize = lambda: self._authorized(source, row['service_revision'], row['role_revision'])
                if not authorize():
                    continue
                updated = self.sessions.append_generation_attachments_once(role_id, session_key, run_id,
                    message_id, row['attempt_id'], json.loads(row['attachments']), connection=connection,
                    authorized=authorize)
                connection.execute("UPDATE novelai_media_outbox SET state='sent',message_id=?,consumed=1,message_synced=1 WHERE attempt_id=?", (message_id, row['attempt_id']))
            if updated is not None:
                return updated
        return next((item for item in self.sessions.list_messages(session_key) if item.id == message_id), None)

    async def claim_channel_reply(self, role_id, session_key, run_id, message_id):
        if not session_key.startswith('thread:'):
            return None
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            rows = connection.execute('''SELECT a.*,o.state AS outbox_state,o.consumed FROM novelai_human_claims c
                JOIN novelai_attempts a ON a.attempt_id=c.attempt_id JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id
                WHERE c.role_id=? AND c.human_key=?''', (role_id, run_id)).fetchall()
            for row in rows:
                if row['status'] != 'succeeded' or row['outbox_state'] != 'pending' or row['consumed']:
                    continue
                source = GenerationContext.from_dict(json.loads(row['origin']))
                if source.origin.session_key != session_key or source.origin.parent_run_id != run_id:
                    raise PermissionError('generation channel reply origin mismatch')
                attachments = json.loads(row['attachments'])
                if len(attachments) > 4 or row['error_code']:
                    connection.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1 WHERE attempt_id=?", (row['attempt_id'],))
                    return None
                authorize = lambda: self._authorized(source, row['service_revision'], row['role_revision'])
                if not authorize():
                    return None
                self.sessions.append_generation_attachments_once(role_id, session_key, run_id, message_id,
                    row['attempt_id'], attachments, connection=connection, authorized=authorize)
                connection.execute("UPDATE novelai_media_outbox SET state='sending',message_id=?,consumed=1 WHERE attempt_id=? AND state='pending' AND consumed=0", (message_id, row['attempt_id']))
                return GenerationReply(row['attempt_id'], role_id, tuple(item['id'] for item in attachments), authorize)
        return None

    async def record_channel_receipt(self, reply: GenerationReply, *, status, delivery_id=None, platform_message_id=None):
        if status not in {'pending', 'sent', 'failed', 'uncertain'}:
            raise ValueError('invalid generation delivery receipt')
        receipt = {'status': status,
            'deliveryAttemptId': delivery_id[:128] if isinstance(delivery_id, str) else None,
            'platformMessageId': platform_message_id[:256] if isinstance(platform_message_id, str) else None}
        with self._lock:
            path = self.root / reply.role_id / (reply.attempt_id + '.receipt.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                previous = json.loads(path.read_text(encoding='utf-8'))
                if previous['status'] == 'sent' or previous['status'] == 'uncertain' and status != 'sent':
                    receipt = previous
            staging = path.with_suffix('.partial')
            try:
                with staging.open('w', encoding='utf-8') as handle:
                    handle.write(canonical(receipt))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staging, path)
            finally:
                staging.unlink(missing_ok=True)
            try:
                self._apply_receipt(reply.attempt_id, receipt)
            except sqlite3.Error:
                pass  # The owned journal repairs SQLite only, never the send.

    def _apply_receipt(self, attempt_id, receipt):
        with self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT state FROM novelai_media_outbox WHERE attempt_id=?', (attempt_id,)).fetchone()
            if row is None or row['state'] == 'sent' or row['state'] == 'uncertain' and receipt['status'] != 'sent':
                return
            connection.execute('UPDATE novelai_media_outbox SET state=?,receipt=?,message_synced=0 WHERE attempt_id=?', (receipt['status'], canonical(receipt), attempt_id))

    async def mark_reply_synced(self, reply: GenerationReply):
        with self.ledger.connect() as connection:
            connection.execute("UPDATE novelai_media_outbox SET message_synced=1 WHERE attempt_id=? AND state IN ('sent','failed','uncertain','pending')", (reply.attempt_id,))

    def _recover_receipt(self, row, source):
        if not source.channel_source:
            return
        path = self.root / row['role_id'] / (row['attempt_id'] + '.receipt.json')
        if path.is_file() and path.stat().st_size <= 4096:
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if (isinstance(receipt, dict) and set(receipt) == {'status', 'deliveryAttemptId', 'platformMessageId'}
                    and receipt['status'] in {'pending', 'sent', 'failed', 'uncertain'}
                    and all(value is None or isinstance(value, str) and len(value) <= 256
                        for key, value in receipt.items() if key != 'status')):
                self._apply_receipt(row['attempt_id'], receipt)
        with self.ledger.connect() as connection:
            outbox = connection.execute('SELECT * FROM novelai_media_outbox WHERE attempt_id=?', (row['attempt_id'],)).fetchone()
        if outbox is None or outbox['message_synced'] or not outbox['message_id'] or not outbox['receipt']:
            return
        receipt = json.loads(outbox['receipt'])
        message = next((item for item in self.sessions.list_messages(source.origin.session_key)
            if item.id == outbox['message_id'] and item.runId == source.origin.parent_run_id
            and item.role == 'assistant' and item.messageType == 'text'), None)
        if message is None:
            return
        metadata = {**message.metadata, 'deliveryStatus': receipt['status'],
            'deliveryAttemptId': receipt['deliveryAttemptId'], 'platformMessageId': receipt['platformMessageId']}
        if receipt['status'] == 'sent':
            metadata.pop('deliveryError', None)
        self.sessions.update_message(message.id, message.content, 'completed' if receipt['status'] == 'sent' else 'failed', metadata=metadata)
        with self.ledger.connect() as connection:
            connection.execute('UPDATE novelai_media_outbox SET message_synced=1 WHERE attempt_id=? AND receipt=?', (row['attempt_id'], outbox['receipt']))

    async def invalidate(self, role_id=None):
        roles = [role_id] if role_id is not None else [role.id for role in self.roles.list()]
        for identifier in roles:
            with self._lock, self.ledger.connect() as connection:
                rows = connection.execute("SELECT a.origin FROM novelai_attempts a JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id WHERE a.role_id=? AND o.state='pending'", (identifier,)).fetchall()
                for row in rows:
                    try:
                        self.seal_request(GenerationContext.from_dict(json.loads(row['origin'])))
                    except OSError:
                        pass  # Revocation remains in memory; cancellation must join.
            await finish_io(asyncio.to_thread(self.tasks.cancel_role_tasks, identifier, kind='novelai_image'))

    def purge_role(self, role_id):
        """Finish successful role deletion; never erase the account's day count."""
        with self._lock:
            if self.roles.get(role_id) is not None:
                raise PermissionError('generation history belongs to an existing role')
            directory = self.root / role_id
            if directory.parent.resolve() != self.root.resolve():
                raise ValueError('generation directory is outside its storage root')
            if directory.is_symlink():
                directory.unlink()
            elif directory.is_junction():
                directory.rmdir()
            elif directory.exists():
                if directory.resolve().parent != self.root.resolve():
                    raise ValueError('generation directory is outside its storage root')
                shutil.rmtree(directory)
            with self.ledger.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('DELETE FROM novelai_media_outbox WHERE attempt_id IN (SELECT attempt_id FROM novelai_attempts WHERE role_id=?)', (role_id,))
                for table in ('novelai_human_claims', 'novelai_attempts', 'novelai_generations', 'novelai_role_epochs', 'novelai_cancelled_requests', 'novelai_daily'):
                    connection.execute('DELETE FROM ' + table + ' WHERE role_id=?', (role_id,))
            self._cancelled_requests = {item for item in self._cancelled_requests if item[0] != role_id}

    async def recover(self):
        """Repair local media/messages and ACK only; never launch or POST again."""
        with self.ledger.connect() as connection:
            rows = connection.execute('''SELECT a.*,o.state AS outbox_state,o.message_synced FROM novelai_attempts a
                JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id
                WHERE a.status IN ('prepared','dispatching','received','local_pending') OR a.runtime_ack_pending=1
                   OR (a.status='unknown' AND a.reserved_bytes>0)
                   OR (o.consumed=1 AND o.message_synced=0)
                   OR (a.status='succeeded' AND o.state='pending') LIMIT 100''').fetchall()
        for saved in rows:
            row = dict(saved)
            source = GenerationContext.from_dict(json.loads(row['origin']))
            if self.is_role_deleting(row['role_id']):
                continue
            if self.roles.get(row['role_id']) is None:
                try:
                    self.purge_role(row['role_id'])
                except (OSError, sqlite3.Error, PermissionError, ValueError):
                    pass
                continue
            try:
                if self._sealed(source):
                    self.seal_request(source)
                self._recover_receipt(row, source)
                task = self._find_task(row, source)
                if task is not None and task.ended_at is None:
                    continue
                if task is not None and not row['task_id']:
                    self.ledger.set_task(row['attempt_id'], task.task_id)
                    row = self.ledger.attempt(row['attempt_id'])
                if row['status'] in {'received', 'local_pending'}:
                    self._import_received(row)
                    row = self.ledger.attempt(row['attempt_id'])
                self._reconcile_terminal(row['attempt_id'])
                row = self.ledger.attempt(row['attempt_id'])
                if row['status'] == 'unknown' and row['reserved_bytes']:
                    self._settle_failure(row)
                    row = self.ledger.attempt(row['attempt_id'])
                if row['runtime_ack_pending'] and row['task_id']:
                    self.tasks.acknowledge_result(row['task_id'], source.origin)
                    with self.ledger.connect() as connection:
                        connection.execute('UPDATE novelai_attempts SET runtime_ack_pending=0 WHERE attempt_id=?', (row['attempt_id'],))
                elif row['runtime_ack_pending'] and task is None:
                    # No Runtime record in the actual source, after successful
                    # authorized enumeration, proves admission never happened.
                    with self.ledger.connect() as connection:
                        connection.execute('UPDATE novelai_attempts SET runtime_ack_pending=0 WHERE attempt_id=?', (row['attempt_id'],))
                if row['status'] != 'succeeded' or not self._authorized(source, row['service_revision'], row['role_revision']):
                    continue
                if source.ui_action:
                    await self.attach_ui_completion(source, row['attempt_id'])
                elif not source.channel_source:
                    final = next((item for item in reversed(self.sessions.list_messages(source.origin.session_key))
                        if item.runId == source.origin.parent_run_id and item.role == 'assistant' and item.messageType == 'text' and item.status == 'completed'), None)
                    run = self.sessions.get_run(source.origin.parent_run_id)
                    if final is not None and run is not None and run.status == 'completed':
                        await self.attach_foreground_reply(row['role_id'], source.origin.session_key, source.origin.parent_run_id, final.id)
            except (OSError, sqlite3.Error, KeyError, ValueError, PermissionError):
                continue

    async def close(self):
        with self._lock:
            self._closed = True
        if self._maintenance is not None:
            self._maintenance.cancel()
            async def join_maintenance():
                await asyncio.gather(self._maintenance, return_exceptions=True)
            await finish_io(join_maintenance())
            self._maintenance = None
        await self.invalidate()

    def tool_for(self, role_id, *, is_owner=True, source_thread_id=None, source_account_id=None, frozen_role=None):
        from .tools import GenerateImageTool
        return GenerateImageTool(self, role_id, is_owner=is_owner, source_thread_id=source_thread_id,
                                 source_account_id=source_account_id, frozen_role=frozen_role)

    def router(self):
        from .api import novelai_router
        return novelai_router(self)
