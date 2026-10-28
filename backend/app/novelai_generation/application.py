"""Own the paid-attempt boundary from real human intent to original-source media."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from ..channels.consolidation import finish_io
from ..channels.models import TargetKind, target_from_chat
from ..chat_attachments import resolve_attachment
from ..session_store import AttachmentConflict
from ..task_runtime import TaskOrigin
from ..task_runtime.runtime import TaskBudget, TaskOutcome
from .config import NovelaiRoleConfig, NovelaiSettings, NovelaiSettingsStore
from .availability import GenerationUnavailable, state
from .context import GenerationContext
from .executor import GenerationExecutor
from .http import GenerationHttpError, NovelaiHttp
from .ledger import GenerationLedger, canonical
from .media import import_response, owned_outputs, validate_image
from .parameters import generation_payload, normalize_request, replay_request
from .scene import SceneCoordinator
from .prompt_tags import PromptTags
from .assets import AssetWriteback


@dataclass(frozen=True)
class GenerationReply:
    attempt_id: str
    role_id: str
    attachment_ids: tuple[str, ...]
    authorized: Callable[[], bool]


class NovelaiApplication:
    def __init__(self, *, root: Path, tasks, sessions, roles, channels, credentials,
                 is_role_deleting=lambda role_id: False, http=None, send_channel=None,
                 channel_media_supported=None, lock_for=None, publish_message=None,
                 model_configuration=None, model_adapter=None):
        self.root, self.tasks, self.sessions, self.roles, self.channels = root, tasks, sessions, roles, channels
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.settings = NovelaiSettingsStore(sessions.database_path, credentials, lock=self._lock)
        self.ledger = GenerationLedger(sessions.database_path)
        self.http = http or NovelaiHttp()
        self.is_role_deleting, self.send_channel = is_role_deleting, send_channel
        self.channel_media_supported = channel_media_supported
        self.lock_for = lock_for
        self.publish_message = publish_message
        self.model_configuration = model_configuration or (lambda identifier: None)
        self.model_adapter = model_adapter
        self._closed = False
        self._maintenance: asyncio.Task[None] | None = None
        self._recovery_lock = threading.Lock()
        self._cancelled_requests: set[tuple[str, str, str]] = set()
        self._pending_channel_receipts: dict[tuple[str, str], dict[str, str | None]] = {}
        self.tags = PromptTags(self)
        self.scenes = SceneCoordinator(self)
        self.assets = AssetWriteback(self)

    def _validate_policy(self, policy):
        if policy.autoWritebackRoleAssets and not policy.sceneCgEnabled:
            raise ValueError('自动写回角色卡片图需要独立开启场景 CG')
        if policy.sceneCgEnabled and (not policy.scenePromptModelConfigurationId
                or self.model_configuration(policy.scenePromptModelConfigurationId) is None):
            raise ValueError('场景 CG 需要明确且仍存在的辅助模型连接')
        if policy.sceneCgEnabled:
            public = self.settings.public()
            if not public['availability']['available'] or not public['allowTxt2Img']:
                raise ValueError('请先启用 NovelAI、配置凭据并允许文生图')

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
        policy = role.agentConfig.novelai.model_dump()
        # New default-off tag fields do not revoke #171/#172 saved requests or
        # local receipt obligations. Explicit permission changes still epoch.
        if not policy['promptTagsEnabled'] and policy['allowedPromptTagRatings'] == ['general']:
            policy.pop('promptTagsEnabled')
            policy.pop('allowedPromptTagRatings')
        if not policy['sceneCgPromptInstructions']:
            policy.pop('sceneCgPromptInstructions')
        return hashlib.sha256(canonical([role.createdAt.isoformat(), row[0] if row else 0, policy]).encode()).hexdigest()

    def tool_enabled(self, role):
        # A configured capability remains discoverable when its token, modes
        # or quota need attention; execution still checks current readiness.
        return bool(not self._closed and role is not None and not self.is_role_deleting(role.id)
                    and self.settings.public()['enabled'])

    def availability(self, role, *, include_usage=True, scene=False):
        config, _ = self._configuration()
        if self._closed or role is None or self.is_role_deleting(role.id):
            return state('role_unavailable', '角色生图服务当前不可用。')
        if not config['availability']['available']:
            return config['availability']
        policy = role.agentConfig.novelai
        if scene and not policy.sceneCgEnabled:
            return state('scene_disabled', '本角色的自动场景 CG 已关闭。')
        if scene and (not policy.scenePromptModelConfigurationId or self.model_configuration(policy.scenePromptModelConfigurationId) is None):
            return state('scene_model_unavailable', '请为本角色选择仍可用的 CG 辅助模型连接。')
        if scene and not config['allowTxt2Img']:
            return state('txt2img_disabled', '文生图已关闭，自动场景 CG 暂不可用。')
        usage = self.ledger.usage(role.id)
        if include_usage and (usage['accountAttempts'] >= config['dailyAttemptLimit'] or
                (usage['cgAttempts'] >= policy.sceneCgDailyAttempts if scene else usage['manualAttempts'] >= config['manualDailyAttempts'])):
            return state('daily_limit', '今日生图尝试额度已用完；请明日再试或在设置中调整上限。次数不是 Anlas 估价。')
        return config['availability']

    def unavailable_reason(self, role, *, include_usage=True, scene=False):
        availability = self.availability(role, include_usage=include_usage, scene=scene)
        return '' if availability['available'] else availability['message']

    def _authorized(self, source: GenerationContext, service_revision, role_revision, *, allow_sealed=False, receipt=False):
        role = self.roles.get(source.origin.role_id)
        public = self.settings.public()
        return bool(not self._closed and (allow_sealed or not self._sealed(source)) and role is not None and not self.is_role_deleting(role.id)
            and public['enabled'] and public['tokenConfigured'] and not public['credentialCleanupPending']
            and public['revision'] == service_revision
            and (source.scene_revision is None or role.agentConfig.novelai.sceneCgEnabled)
            and self._role_revision(role) == role_revision and source.denial(self.channels) is None
            and (receipt or self.scenes.current(source)))

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
        try:
            with path.open('rb') as handle:
                content = handle.read(10 * 1024 * 1024 + 1)
        except (FileNotFoundError, NotADirectoryError):
            raise PermissionError('img2img input is no longer available in this role') from None
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError('img2img input exceeds 10 MiB')
        validate_image(content, max_pixels=16 * 1024 * 1024)
        return content

    def _expand_chat_prompt_tags(self, source: GenerationContext, role, arguments):
        """Apply the role tag library to a real owner-turn expression request."""
        policy = role.agentConfig.novelai
        if source.ui_action or not policy.promptTagsEnabled:
            return arguments
        message = self.sessions.user_message_for_run(source.human_key)
        if message is None or message.sessionKey != source.origin.session_key:
            raise PermissionError('current owner input is unavailable for prompt tag expansion')
        expanded = self.tags.expand(role.id, message.content,
            arguments.get('prompt', ''), arguments.get('negative_prompt', ''))
        return {**arguments, 'prompt': expanded['prompt'], 'negative_prompt': expanded['negative_prompt']}

    def _find_task(self, row, source):
        if row['task_id']:
            try:
                return self.tasks.query(row['task_id'], source.origin)
            except KeyError:
                return None
        marker = 'NovelAI attempt:' + row['attempt_id']
        return next((item for item in self.tasks.list(source.origin, kind='novelai_image') if item.description == marker), None)

    async def submit(self, source: GenerationContext, arguments: dict[str, object], *, signal=None, expected_revision=None,
                     expected_role_revision=None, fingerprint=None, regeneration=None, publication_ready=True,
                     delivery_mode=None, scene_input=None):
        handover_error = None
        with self._lock:
            if not source.ui_action and {'qualityToggle', 'ucPreset'} & arguments.keys():
                raise PermissionError('per-image preference overrides require a local owner action')
            role = self.roles.get(source.origin.role_id)
            availability = self.availability(role, include_usage=False, scene=source.scene_revision is not None)
            if not availability['available']:
                raise GenerationUnavailable(availability)
            if expected_role_revision is not None and self._role_revision(role) != expected_role_revision:
                raise PermissionError('frozen generation role permission changed')
            if source.denial(self.channels):
                raise PermissionError('original generation source was revoked')
            if source.channel_source is not None and delivery_mode != 'history_only' and (self.channel_media_supported is None
                    or not self.channel_media_supported(source.origin.source_account_id)):
                raise PermissionError('original channel cannot automatically deliver image media')
            if (source.channel_source is not None and delivery_mode != 'history_only'
                    and (arguments.get('background') is True or regeneration is not None) and self.send_channel is None):
                raise PermissionError('original channel managed media sender is unavailable')
            public, settings = self._configuration()
            if expected_revision is not None and public['revision'] != expected_revision:
                raise ValueError('generation service revision changed')
            if scene_input is not None and not settings.allowTxt2Img:
                raise PermissionError('scene CG requires txt2img mode to be enabled')
            # Preserve #171's saved UI/tool identity when no new mode was
            # requested. Explicit delivery choices remain part of the new key.
            fingerprint = fingerprint or hashlib.sha256(canonical(arguments if delivery_mode is None else
                {'parameters': arguments, 'deliveryMode': delivery_mode}).encode()).hexdigest()
            row = self.ledger.request(role.id, source.source_scope, source.request_key)
            existing = row is not None
            owned_input = None
            if existing:
                if row['fingerprint'] != fingerprint or row['origin'] != canonical(source.to_dict()):
                    raise ValueError('request parameters conflict with saved request')
            else:
                if self._sealed(source):
                    raise PermissionError('generation request was cancelled')
                if scene_input is not None and (source.scene_revision is None or not self.scenes.current(source)):
                    raise PermissionError('scene generation requires a current host candidate')
                chat_arguments = arguments
                if scene_input is None:
                    chat_arguments = self._expand_chat_prompt_tags(source, role, arguments)
                parameters: dict[str, object] = ({
                    'background': True,
                    # Scene preferences are captured with the candidate. The
                    # auxiliary model may finish later, so it must not read a
                    # newer service configuration while committing its image.
                    'qualityToggle': settings.addQualityTags,
                    'ucPreset': settings.undesiredContentPreset,
                } if scene_input is not None else normalize_request(chat_arguments, settings))
                if regeneration is not None and delivery_mode == 'replacement' and parameters.get('samples', 1) != 1:
                    raise ValueError('replacement delivery supports exactly one generated image')
                owned_input = None if scene_input is not None else self._input(source, parameters)
                if owned_input is not None:
                    parameters['inputSha256'] = hashlib.sha256(owned_input).hexdigest()
                view = self.ledger.prepare(role_id=role.id, source_scope=source.source_scope,
                    human_key=source.human_key, request_key=source.request_key, parameters=parameters,
                    origin=source.to_dict(), service_revision=public['revision'], role_revision=self._role_revision(role),
                    account_limit=settings.dailyAttemptLimit, role_limit=(role.agentConfig.novelai.sceneCgDailyAttempts
                        if scene_input is not None else settings.manualDailyAttempts),
                    fingerprint=fingerprint, delivery_mode=delivery_mode or ('replacement' if regeneration is not None else
                        'source_completion' if source.ui_action or parameters['background'] else 'foreground_reply'),
                    publication_ready=publication_ready,
                    scene_input=scene_input,
                    asset_writeback_requested=scene_input is not None and role.agentConfig.novelai.autoWritebackRoleAssets,
                    **(regeneration or {}))
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
        if json.loads(row['parameters'])['background'] and task is not None:
            # Acceptance transfers publication responsibility to this business
            # outbox, only after the original Runtime handover is durably copied.
            try:
                if signal is not None:
                    signal.raise_if_cancelled()
                with self._lock, self.ledger.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    if not self._authorized(source, row['service_revision'], row['role_revision']):
                        raise PermissionError('background source was revoked before acceptance')
                    connection.execute('''UPDATE novelai_media_outbox SET background_accepted=1,publication_ready=1
                        WHERE attempt_id=? AND consumed=0''', (row['attempt_id'],))
            except (asyncio.CancelledError, PermissionError, OSError, sqlite3.Error):
                try:
                    self.seal_request(source)
                finally:
                    await finish_io(asyncio.to_thread(self.tasks.stop, task.task_id, source.origin))
                    self._reconcile_terminal(row['attempt_id'])
                raise
            return self.ledger.get(role.id, source.source_scope, row['generation_id'], attempt_id=row['attempt_id'])
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
        return self.ledger.get(role.id, source.source_scope, row['generation_id'], attempt_id=row['attempt_id'])

    async def _execute(self, row, source, owned_input, execution):
        spool = self.root / row['role_id'] / (row['attempt_id'] + '.response')
        try:
            if source.scene_revision is not None:
                if not await self.scenes.observe(row, source, execution):
                    return TaskOutcome('succeeded')
                row = self.ledger.attempt(row['attempt_id'])
            with self._lock:
                if not self._authorized(source, row['service_revision'], row['role_revision']):
                    self.ledger.finish(row['attempt_id'], 'cancelled_before_dispatch')
                    return TaskOutcome('cancelled')
                token = self.settings.dispatch_token()
                payload = generation_payload(json.loads(row['parameters']), base_image=owned_input)
                self.ledger.mark_dispatching(row['attempt_id'], before_dispatch=(
                    lambda connection: self.scenes.dispatch(connection, row, source)) if source.scene_revision is not None else None)
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
                    path_value = item.get('path') if item is not None else None
                    if item is None or not isinstance(path_value, str) or item['size'] != attachment['size'] or Path(path_value).stat().st_size != attachment['size']:
                        raise OSError('previously imported image is unavailable')
                    validate_image(Path(path_value).read_bytes(), max_pixels=max_pixels)
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
        if 'width' not in parameters:
            self.ledger.settled_failure(row['attempt_id'], [], 0)
            return
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

    def owner_generation(self, role_id, generation_id, *, attempt_id=None):
        if self.roles.get(role_id) is None or self.is_role_deleting(role_id):
            raise KeyError('role unavailable')
        with self.ledger.connect() as connection:
            row = connection.execute('SELECT source_scope FROM novelai_generations WHERE generation_id=? AND role_id=?', (generation_id, role_id)).fetchone()
        if row is None:
            raise KeyError('generation unavailable')
        return self.ledger.get(role_id, row[0], generation_id, attempt_id=attempt_id)

    def attachment_generation(self, role_id, attachment_id, message_id):
        """Resolve a real currently displayed image through the canonical outbox."""
        if self.roles.get(role_id) is None or self.is_role_deleting(role_id):
            raise KeyError('role unavailable')
        with self.ledger.connect() as connection:
            has_threads = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='channel_threads'").fetchone()
            thread_scope = ('EXISTS(SELECT 1 FROM channel_threads t WHERE t.session_key=m.session_key AND t.role_id=a.role_id)'
                if has_threads else '0')
            row = connection.execute('''SELECT a.generation_id,a.attempt_id,a.source_scope FROM
                novelai_media_outbox o JOIN novelai_attempts a ON a.attempt_id=o.attempt_id
                JOIN messages m ON m.message_id=o.message_id
                WHERE a.role_id=? AND o.message_id=? AND m.role='assistant' AND m.status='completed'
                AND m.session_key=json_extract(a.origin,'$.origin.sessionKey')
                AND EXISTS(SELECT 1 FROM json_each(a.attachments) WHERE json_extract(value,'$.id')=?)
                AND EXISTS(SELECT 1 FROM json_each(m.metadata_json,'$.attachments')
                    WHERE json_extract(value,'$.id')=? AND json_extract(value,'$.kind')='image')
                AND EXISTS(SELECT 1 FROM json_each(m.metadata_json,'$.generationAttemptIds') WHERE value=a.attempt_id)
                AND (EXISTS(SELECT 1 FROM sessions s WHERE s.session_key=m.session_key AND s.role_id=a.role_id)
                    OR ''' + thread_scope + ''') ORDER BY a.created_at DESC,a.attempt_id DESC LIMIT 1''',
                (role_id, message_id, attachment_id, attachment_id)).fetchone()
        if row is None:
            raise KeyError('displayed owned generation image unavailable')
        return self.ledger.get(role_id, row['source_scope'], row['generation_id'], attempt_id=row['attempt_id'])

    def regeneration_source(self, role_id, generation_id, *, source_attempt_id, request_id, parameters):
        view = self.owner_generation(role_id, generation_id, attempt_id=source_attempt_id)
        original = GenerationContext.from_dict(json.loads(self.ledger.attempt(view['attemptId'])['origin']))
        origin = original.origin
        identifier = replay_request(view['parameters'], parameters).get('base_attachment_id')
        # A desktop owner may choose a current owned image, but managing channel
        # history cannot expand the original host-captured channel input grant.
        attachment_ids = (original.attachment_ids if original.channel_source is not None
            else frozenset([identifier]) if isinstance(identifier, str) else frozenset())
        return GenerationContext(type(origin)(role_id, origin.session_key,
            source_thread_id=origin.source_thread_id, source_account_id=origin.source_account_id,
            source_scope_id=origin.source_scope_id, source_authority_json=origin.source_authority_json),
            'ui:' + request_id, 'ui:' + request_id,
            attachment_ids,
            original.channel_source, original.channel_revision, ui_action=True,
            reply_to_message_id=original.reply_to_message_id,
            message_thread_id=original.message_thread_id, mention_ids=original.mention_ids)

    async def regenerate(self, source, generation_id, *, source_attempt_id, expected_attachment_id,
                         arguments, expected_revision, publication_ready=True, delivery_mode=None):
        parent = self.owner_generation(source.origin.role_id, generation_id, attempt_id=source_attempt_id)
        if expected_attachment_id not in {item['id'] for item in parent['attachments']}:
            raise PermissionError('selected image does not belong to the original generation')
        operation = {'generation_id': generation_id, 'parent_attempt_id': source_attempt_id,
            'message_id': parent['deliveryMessageId'], 'expected_attachment_id': expected_attachment_id}
        fingerprint = hashlib.sha256(canonical({'parameters': arguments, 'regeneration': operation,
            'deliveryMode': delivery_mode}).encode()).hexdigest()
        previous = self.ledger.request(source.origin.role_id, source.source_scope, source.request_key)
        parameters = replay_request(parent['parameters'], arguments)
        if previous is None:
            if delivery_mode != 'history_only':
                try:
                    displayed = self.attachment_generation(source.origin.role_id,
                        expected_attachment_id, parent['deliveryMessageId'])
                except KeyError:
                    raise AttachmentConflict('original image slot is no longer available') from None
                if displayed['attemptId'] != source_attempt_id:
                    raise AttachmentConflict('original image provenance no longer matches')
            if arguments.get('seed') is None:
                seed = secrets.randbelow(2**32)
                while seed == parent['parameters']['seed']:
                    seed = secrets.randbelow(2**32)
                parameters['seed'] = seed
            elif arguments['seed'] == parent['parameters']['seed']:
                raise ValueError('regeneration requires a new seed')
        result = await self.submit(source, parameters, expected_revision=expected_revision,
            fingerprint=fingerprint, regeneration=operation, publication_ready=publication_ready, delivery_mode=delivery_mode)
        if result['status'] == 'succeeded' and publication_ready and not result['parameters']['background']:
            await self.attach_replacement(source, result['attemptId'])
        return self.owner_generation(source.origin.role_id, generation_id, attempt_id=result['attemptId'])

    def keep_history_only(self, attempt_id):
        with self.ledger.connect() as connection:
            connection.execute("""UPDATE novelai_media_outbox SET state='history_only',consumed=1,message_synced=1
                WHERE attempt_id=? AND delivery_mode='history_only' AND state='pending' AND publication_ready=1""", (attempt_id,))

    def allow_ui_publication(self, source, attempt_id):
        """The live foreground HTTP action alone releases its durable publish gate."""
        if not source.ui_action:
            raise PermissionError('only the actual owner UI action may release publication')
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM novelai_attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if (row is None or row['origin'] != canonical(source.to_dict())
                    or not self._authorized(source, row['service_revision'], row['role_revision'])):
                raise PermissionError('original owner publication action was revoked')
            connection.execute('UPDATE novelai_media_outbox SET publication_ready=1 WHERE attempt_id=? AND consumed=0', (attempt_id,))

    async def attach_replacement(self, source, attempt_id):
        return await self._publish_owned(source, attempt_id, replacement=True)

    def _attach_replacement(self, source, attempt_id):
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT a.*,o.state AS outbox_state,o.consumed,o.message_id,
                o.expected_attachment_id,o.delivery_mode,o.publication_ready FROM novelai_attempts a
                JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id WHERE a.attempt_id=?''', (attempt_id,)).fetchone()
            if row is None or row['status'] != 'succeeded' or row['origin'] != canonical(source.to_dict()):
                raise PermissionError('replacement does not match its original generation action')
            if (row['delivery_mode'] != 'replacement' or not row['publication_ready']
                    or row['outbox_state'] not in {'pending', 'sent'}):
                return None
            authorize = lambda: self._authorized(source, row['service_revision'], row['role_revision'])
            if not authorize():
                raise PermissionError('replacement source was revoked')
            attachments = json.loads(row['attachments'])
            if row['consumed']:
                return None
            try:
                if row['error_code']:
                    connection.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1,message_synced=1 WHERE attempt_id=?", (attempt_id,))
                    return None
                if len(attachments) != 1 or not row['message_id']:
                    raise AttachmentConflict('generated image has no available original slot')
                message = self.sessions.replace_attachment_once(row['role_id'], source.origin.session_key,
                    row['message_id'], row['expected_attachment_id'], attachments[0], attempt_id,
                    connection=connection, authorized=authorize)
            except AttachmentConflict:
                connection.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1,message_synced=1 WHERE attempt_id=?", (attempt_id,))
                connection.execute("UPDATE novelai_attempts SET error_code='replacement_conflict' WHERE attempt_id=?", (attempt_id,))
                return None
            if source.channel_source is not None:
                connection.execute("UPDATE novelai_media_outbox SET state='sending',consumed=1,message_synced=0 WHERE attempt_id=?", (attempt_id,))
                return GenerationReply(attempt_id, row['role_id'], tuple(item['id'] for item in attachments), authorize)
            connection.execute("UPDATE novelai_media_outbox SET state='sent',consumed=1,message_synced=1 WHERE attempt_id=?", (attempt_id,))
            return message

    async def cancel(self, role_id, generation_id, *, attempt_id=None):
        view = self.owner_generation(role_id, generation_id, attempt_id=attempt_id)
        row = self.ledger.attempt(view['attemptId'])
        source = GenerationContext.from_dict(json.loads(row['origin']))
        try:
            self.seal_request(source)
        finally:
            task = self._find_task(row, source)
            if task is not None and task.ended_at is None:
                await finish_io(asyncio.to_thread(self.tasks.stop, task.task_id, source.origin))
            self._reconcile_terminal(row['attempt_id'])
        return self.owner_generation(role_id, generation_id, attempt_id=row['attempt_id'])

    async def attach_ui_completion(self, source: GenerationContext, attempt_id):
        return await self._publish_owned(source, attempt_id)

    async def _publish_owned(self, source, attempt_id, *, replacement=False):
        lock = self.lock_for(source.origin.role_id) if self.lock_for else None
        if lock is not None:
            try:
                await asyncio.wait_for(lock.acquire(), .01)
            except TimeoutError:
                return None
        try:
            if replacement:
                result = self._attach_replacement(source, attempt_id)
            elif source.channel_source is not None:
                result = self._claim_channel_completion(source, attempt_id)
            else:
                result = self._attach_source_completion(source, attempt_id)
            if isinstance(result, GenerationReply):
                await finish_io(self._send_source_media(source, result))
            return result
        finally:
            if lock is not None:
                lock.release()

    def _claim_channel_completion(self, source, attempt_id):
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('''SELECT a.*,o.state AS outbox_state,o.consumed,o.publication_ready,
                o.background_accepted,o.completion_fallback FROM novelai_attempts a JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id
                WHERE a.attempt_id=? AND o.delivery_mode='source_completion' ''', (attempt_id,)).fetchone()
            if row is None or row['status'] != 'succeeded' or row['origin'] != canonical(source.to_dict()):
                raise PermissionError('channel completion does not match original source')
            if not row['publication_ready'] or row['consumed'] or row['outbox_state'] != 'pending':
                return None
            if not row['background_accepted'] and not row['completion_fallback']:
                raise PermissionError('channel completion requires accepted managed work')
            if row['error_code']:
                connection.execute("UPDATE novelai_media_outbox SET state='history_only',consumed=1,message_synced=1 WHERE attempt_id=?", (attempt_id,))
                return None
            authorize = lambda: self._authorized(source, row['service_revision'], row['role_revision'])
            if not authorize():
                raise PermissionError('channel completion source was revoked')
            attachments = json.loads(row['attachments'])
            message = self.sessions.append_generation_completion_once(row['role_id'], source.origin.session_key,
                attempt_id, attachments, connection=connection, authorized=authorize,
                channel=source.channel_source.channel, delivery_status='pending')
            connection.execute("UPDATE novelai_media_outbox SET state='sending',message_id=?,consumed=1,message_synced=0 WHERE attempt_id=?", (message.id, attempt_id))
            return GenerationReply(attempt_id, row['role_id'], tuple(item['id'] for item in attachments), authorize)

    async def _send_source_media(self, source, reply):
        channel = source.channel_source
        target = target_from_chat(account_id=channel.account_id, chat_id=channel.chat_id,
            kind=TargetKind.GROUP if channel.chat_type == 'group' else TargetKind.PRIVATE)
        try:
            if self.send_channel is None:
                raise PermissionError('original media sender unavailable')
            result = await self.send_channel(source.origin.role_id, channel.account_id, target.id, '图片已生成',
                actor='reply', record_message=False, attachment_ids=reply.attachment_ids,
                source_thread_id=channel.thread_id, source_session_key=source.origin.session_key,
                source_run_id=source.origin.parent_run_id or None, source_tool_call_id=source.origin.tool_call_id,
                source_kind='novelai_generation', authorize_source=reply.authorized,
                reply_to_message_id=source.reply_to_message_id, message_thread_id=source.message_thread_id,
                mention_ids=source.mention_ids)
            await self.record_channel_receipt(reply, status=result.status.value,
                delivery_id=result.attempt_id, platform_message_id=result.platform_message_id)
        except asyncio.CancelledError:
            await self.record_channel_receipt(reply, status='uncertain')
            raise
        except (OSError, ValueError, PermissionError, KeyError, RuntimeError):
            await self.record_channel_receipt(reply, status='uncertain')
        self._recover_receipt(self.ledger.attempt(reply.attempt_id), source)

    def _attach_source_completion(self, source, attempt_id):
        with self._lock, self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT a.*,o.state AS outbox_state,o.consumed,o.publication_ready,o.background_accepted,o.completion_fallback FROM novelai_attempts a JOIN novelai_media_outbox o ON o.attempt_id=a.attempt_id WHERE a.attempt_id=? AND o.delivery_mode='source_completion'", (attempt_id,)).fetchone()
            if row is None or row['status'] != 'succeeded' or row['origin'] != canonical(source.to_dict()):
                raise PermissionError('generation completion does not match original UI action')
            if not source.ui_action and not row['background_accepted'] and not row['completion_fallback']:
                raise PermissionError('source completion requires an accepted managed submission')
            if row['outbox_state'] not in {'pending', 'sent'}:
                return None
            if not row['publication_ready']:
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
                WHERE c.role_id=? AND c.source_scope='desktop' AND c.human_key=? AND o.delivery_mode='foreground_reply' ''', (role_id, run_id)).fetchall()
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
                WHERE c.role_id=? AND c.human_key=? AND o.delivery_mode='foreground_reply' ''', (role_id, run_id)).fetchall()
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
        try:
            self._persist_channel_receipt(reply.role_id, reply.attempt_id, receipt)
        except OSError:
            pass  # A local journal failure cannot change the actual send result.

    def _persist_channel_receipt(self, role_id, attempt_id, receipt):
        with self._lock:
            key = (role_id, attempt_id)
            previous = self._pending_channel_receipts.get(key)
            if previous is not None and (previous['status'] == 'sent'
                    or previous['status'] == 'uncertain' and receipt['status'] != 'sent'):
                receipt = previous
            # Registered only after the sender returns. Recovery must never
            # infer completion merely from a live sending claim without this.
            self._pending_channel_receipts[key] = receipt
            path = self.root / role_id / (attempt_id + '.receipt.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                previous = json.loads(path.read_text(encoding='utf-8'))
                if previous['status'] == 'sent' or previous['status'] == 'uncertain' and receipt['status'] != 'sent':
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
            self._pending_channel_receipts.pop(key, None)
            try:
                self._apply_receipt(attempt_id, receipt)
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
        with self._lock:
            receipt = self._pending_channel_receipts.get((row['role_id'], row['attempt_id']))
            if receipt is not None:
                self._persist_channel_receipt(row['role_id'], row['attempt_id'], receipt)
        path = self.root / row['role_id'] / (row['attempt_id'] + '.receipt.json')
        if path.is_file() and path.stat().st_size <= 4096:
            receipt = json.loads(path.read_text(encoding='utf-8'))
            if (isinstance(receipt, dict) and set(receipt) == {'status', 'deliveryAttemptId', 'platformMessageId'}
                    and receipt['status'] in {'pending', 'sent', 'failed', 'uncertain'}
                    and all(value is None or isinstance(value, str) and len(value) <= 256
                        for key, value in receipt.items() if key != 'status')):
                self._apply_receipt(row['attempt_id'], receipt)
        with self.ledger.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            outbox = connection.execute('SELECT * FROM novelai_media_outbox WHERE attempt_id=?', (row['attempt_id'],)).fetchone()
            if outbox is not None and outbox['state'] == 'uncertain' and not outbox['receipt']:
                # Restart knows a sending claim existed, not whether the peer
                # accepted it. Project that uncertainty without issuing a send.
                connection.execute('UPDATE novelai_media_outbox SET receipt=? WHERE attempt_id=?',
                    (canonical({'status': 'uncertain', 'deliveryAttemptId': None, 'platformMessageId': None}), row['attempt_id']))
                outbox = connection.execute('SELECT * FROM novelai_media_outbox WHERE attempt_id=?', (row['attempt_id'],)).fetchone()
            if outbox is None or outbox['message_synced'] or not outbox['message_id'] or not outbox['receipt']:
                return
            receipt = json.loads(outbox['receipt'])
            message = self.sessions.reconcile_generation_receipt(row['role_id'], source.origin.session_key,
                outbox['message_id'], row['attempt_id'], receipt, connection=connection,
                authorized=lambda: self._authorized(source, row['service_revision'], row['role_revision'], receipt=True))
            if message is not None:
                connection.execute('UPDATE novelai_media_outbox SET message_synced=1 WHERE attempt_id=? AND receipt=?', (row['attempt_id'], outbox['receipt']))
        if message is not None and self.publish_message is not None:
            self.publish_message(row['role_id'], message)

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
                for table in ('novelai_cg_daily', 'novelai_asset_usage', 'novelai_asset_writes', 'novelai_prompt_tags',
                        'novelai_tag_revisions', 'novelai_scene_state', 'novelai_scene_humans', 'novelai_scene_candidates',
                        'novelai_scene_manual', 'novelai_scene_visuals'):
                    connection.execute('DELETE FROM ' + table + ' WHERE role_id=?', (role_id,))
            self._cancelled_requests = {item for item in self._cancelled_requests if item[0] != role_id}
            self._pending_channel_receipts = {key: receipt for key, receipt in self._pending_channel_receipts.items() if key[0] != role_id}

    async def recover(self):
        """Repair local media/messages and ACK only; never launch or POST again."""
        if not self._recovery_lock.acquire(blocking=False):
            return
        try:
            await self._recover_batch()
        finally:
            self._recovery_lock.release()

    async def _repair_foreground(self, row, source):
        """Wait for the real role turn to release ownership before choosing fallback."""
        lock = self.lock_for(row['role_id']) if self.lock_for else None
        if lock is not None:
            try:
                await asyncio.wait_for(lock.acquire(), .01)
            except TimeoutError:
                return
        try:
            run = self.sessions.get_run(source.origin.parent_run_id)
            if run is not None and run.status in {'created', 'running'}:
                return
            final = next((item for item in reversed(self.sessions.list_messages(source.origin.session_key))
                if item.runId == source.origin.parent_run_id and item.role == 'assistant'
                and item.messageType == 'text' and item.status == 'completed'), None)
            if not source.channel_source and final is not None and run is not None and run.status == 'completed':
                await self.attach_foreground_reply(row['role_id'], source.origin.session_key, source.origin.parent_run_id, final.id)
                return
            with self._lock, self.ledger.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                if not self._authorized(source, row['service_revision'], row['role_revision']):
                    return
                changed = connection.execute("""UPDATE novelai_media_outbox SET delivery_mode='source_completion',
                    completion_fallback=1 WHERE attempt_id=? AND delivery_mode='foreground_reply'
                    AND state='pending' AND consumed=0""", (row['attempt_id'],)).rowcount
            if not changed:
                return
            if source.channel_source:
                reply = self._claim_channel_completion(source, row['attempt_id'])
                if reply is not None:
                    await finish_io(self._send_source_media(source, reply))
            else:
                self._attach_source_completion(source, row['attempt_id'])
        finally:
            if lock is not None:
                lock.release()

    async def _recover_batch(self):
        self.assets.recover()
        rows = self.ledger.recovery_batch()
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
                if row['status'] != 'succeeded':
                    continue
                if not self._authorized(source, row['service_revision'], row['role_revision']):
                    self.assets.apply(row, source)  # Settle the original duty as failed; never revive a grant.
                    continue
                if row['delivery_mode'] == 'replacement':
                    await self.attach_replacement(source, row['attempt_id'])
                elif row['delivery_mode'] == 'history_only':
                    self.keep_history_only(row['attempt_id'])
                elif row['delivery_mode'] == 'source_completion':
                    await self.attach_ui_completion(source, row['attempt_id'])
                elif row['delivery_mode'] == 'foreground_reply':
                    await self._repair_foreground(row, source)
                self.assets.apply(row, source)
            except (OSError, sqlite3.Error, KeyError, ValueError, PermissionError):
                continue

    async def close(self):
        with self._lock:
            self._closed = True
        await self.scenes.close()
        if self._maintenance is not None:
            maintenance = self._maintenance
            maintenance.cancel()
            async def join_maintenance():
                await asyncio.gather(maintenance, return_exceptions=True)
            await finish_io(join_maintenance())
            self._maintenance = None
        await self.invalidate()
        if self.scenes.failures:
            raise ExceptionGroup('scene coordination failed', self.scenes.failures)

    def tool_for(self, role_id, *, is_owner=True, source_thread_id=None, source_account_id=None, frozen_role=None):
        from .tools import GenerateImageTool
        return GenerateImageTool(self, role_id, is_owner=is_owner, source_thread_id=source_thread_id,
                                 source_account_id=source_account_id, frozen_role=frozen_role)

    def router(self):
        from .api import novelai_router
        return novelai_router(self)
