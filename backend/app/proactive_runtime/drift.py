"""A finite Drift mode inside the original autonomous tick and Runtime owner."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
from pathlib import PurePosixPath
import time
from typing import cast
import uuid

from ..agent_runtime.capabilities import CapabilityRegistry
from ..agent_runtime.discovery import ServiceGrant
from ..agent_runtime.file_tools import build_file_tools
from ..agent_runtime.provider import CancellationToken
from ..agent_runtime.tools import AgentToolResult, ToolContext, ToolDefinition, ToolRegistry
from ..ai.model_types import ModelSystemMessage, ModelUserMessage, ModelTextBlock, ModelImageBlock, ModelToolCallBlock, ModelToolResultMessage
from ..task_runtime import TaskOutcome
from .bindings import persona
from .source_authoring import _directory_identity
from .turn import _request
from . import retention
from .tool_schemas import SCHEMAS
from . import drift_resources, drift_shell


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _definition(name, description, properties, required):
    return ToolDefinition(name, description, {'type': 'object', 'properties': properties,
        'required': required, 'additionalProperties': False}, source='autonomy:drift')


SPECIAL = (
    _definition('meme_copy_image', 'Copy one original run-frozen uploaded image into an explicitly granted meme category; host chooses the new filename.',
        {'attachment_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
         'category': {'type': 'string', 'minLength': 1, 'maxLength': 128}}, ['attachment_id', 'category']),
    _definition('shell', 'Run one frozen host read-only argv preset inside this tick.',
        {'rule_id': {'type': 'string', 'minLength': 1, 'maxLength': 64}}, ['rule_id']),
    *(_definition(item['function']['name'], item['function']['description'],
        item['function']['parameters']['properties'], item['function']['parameters']['required'])
        for item in SCHEMAS if item['function']['name'] in {'recall_memory', 'web_fetch', 'web_search'}),
    _definition('message_push', 'Use this tick\'s only send opportunity within its original observed target grants.', {
        'message': {'type': 'string', 'minLength': 1, 'maxLength': 4000},
        'mood': {'type': 'string', 'maxLength': 100}, 'thought': {'type': 'string', 'maxLength': 1000},
        'attachment_ids': {'type': 'array', 'maxItems': 4, 'uniqueItems': True,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 128}},
        'account_id': {'type': 'string', 'minLength': 1, 'maxLength': 256},
        'target_id': {'type': 'string', 'minLength': 1, 'maxLength': 256}}, ['message', 'mood', 'thought']),
    _definition('activate_skill', 'Explicitly activate a trusted skill from this run catalog.',
        {'skill_id': {'type': 'string', 'minLength': 1, 'maxLength': 64}}, ['skill_id']),
    _definition('mount_server', 'Mount only an existing server frozen in this autonomous run.',
        {'server_id': {'type': 'string', 'minLength': 1, 'maxLength': 24}}, ['server_id']),
    _definition('finish_drift', 'Commit actual progress and the exact real message outcome once.', {
        'skill_used': {'type': 'string', 'minLength': 1, 'maxLength': 64},
        'one_line': {'type': 'string', 'minLength': 1, 'maxLength': 300},
        'next_action': {'type': 'string', 'minLength': 1, 'maxLength': 300},
        'message_result': {'type': 'string', 'enum': ['sent', 'silent', 'failed', 'uncertain']}},
        ['skill_used', 'one_line', 'next_action', 'message_result']),
)


class DriftTurn:
    def __init__(self, app, origin, config, snapshot, execution, bundle=None):
        self.app, self.origin, self.config, self.snapshot, self.execution = app, origin, config, snapshot, execution
        self.root = app.drift_skills.workspace(origin.role_id) / 'drift'
        self.skill = self.manifest = None
        self.bundle = bundle
        self.catalog = app.drift_skills.list(origin.role_id)
        self.bundle_hash = None
        self.pending_images = ()
        self.meme_scope = app.memes.freeze(self)
        self.signal = CancellationToken()
        self.processes, self.process_tasks = set(), set()
        self.shell_presets = drift_shell.presets()
        self.resources = drift_resources.freeze(app, origin)
        from .drift_attachments import bind
        self.attachments = {identifier: bind(app, origin.role_id, identifier) for identifier in config.driftAttachmentIds}
        self.history_tools = drift_resources.history_tools(self)
        self.file_tools = {tool.definition.name: tool for tool in build_file_tools(origin.role_id, self.root,
            is_owner=False, multimodal=False, authorized=self.file_authorized, max_file_bytes=8 * 1024 * 1024,
            write_policy=app.drift_files.policy(origin.role_id))}
        self.definitions = {item.name: item for item in (*SPECIAL,
            *(tool.definition for tool in (*self.file_tools.values(), *self.history_tools.values())))}
        with app._transaction() as db:
            now = app._now(db)
            run = db.execute('SELECT * FROM autonomy_runs WHERE tick_id=?', (origin.tick_id,)).fetchone()
            role = db.execute('SELECT * FROM autonomy_roles WHERE role_id=?', (origin.role_id,)).fetchone()
            day = int(run['started_at'] // 86400)
            daily = db.execute('SELECT drifts FROM autonomy_budgets WHERE role_id=? AND day=?', (origin.role_id, day)).fetchone()
            if (not app._authorized_in(db, origin) or 'drift' not in origin.allowed_modes or run['mode'] != 'pending'
                    or not (config.driftAuthoring.enabled or config.driftMeme.enabled) or role['human_busy'] or now - role['last_human_end'] < 600
                    or role['cleanup_pending'] or daily is None or daily[0] >= 4
                    or role['last_drift_at'] is not None and now - role['last_drift_at'] < config.driftMinIntervalHours * 3600):
                raise PermissionError('Drift admission, author scope, or original cooldown is unavailable')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_turns (
                tick_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,active_skill TEXT,bundle_hash TEXT,
                finish TEXT,receipt TEXT,steps INTEGER NOT NULL DEFAULT 0,tokens INTEGER NOT NULL DEFAULT 0,
                input_bytes INTEGER NOT NULL DEFAULT 0)''')
            db.execute('INSERT INTO autonomy_drift_turns(tick_id,role_id) VALUES(?,?)', (origin.tick_id, origin.role_id))
            columns = {row[1] for row in db.execute('PRAGMA table_info(autonomy_drift_turns)')}
            for column in ('image_occurrences', 'image_base64_bytes', 'image_input_estimate', 'envelope_bytes', 'wire_body_bytes', 'wire_requests'):
                if column not in columns:
                    db.execute('ALTER TABLE autonomy_drift_turns ADD COLUMN ' + column + ' INTEGER NOT NULL DEFAULT 0')
            db.execute('INSERT INTO autonomy_drift_logs VALUES(?,?,?)', (origin.tick_id, origin.role_id, 64 * 1024))
            if db.execute("UPDATE autonomy_runs SET mode='drift',status='running' WHERE tick_id=? AND mode='pending'", (origin.tick_id,)).rowcount != 1:
                raise PermissionError('autonomous mode already selected')
            db.execute('UPDATE autonomy_roles SET last_drift_at=? WHERE role_id=?', (now, origin.role_id))
            db.execute('UPDATE autonomy_budgets SET drifts=drifts+1 WHERE role_id=? AND day=?', (origin.role_id, day))
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_capability_revisions (
                tick_id TEXT NOT NULL,role_id TEXT NOT NULL,version INTEGER NOT NULL,revision_json TEXT NOT NULL,
                PRIMARY KEY(tick_id,version))''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_tool_calls (
                tick_id TEXT NOT NULL,role_id TEXT NOT NULL,call_id TEXT NOT NULL,tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,snapshot_id TEXT NOT NULL,result TEXT,is_error INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(tick_id,call_id))''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(autonomy_drift_tool_calls)')}
            if 'binding_json' not in columns:
                db.execute("ALTER TABLE autonomy_drift_tool_calls ADD COLUMN binding_json TEXT")
        self.root_identity = app.drift_files.admit(origin.role_id)
        grants = tuple(ServiceGrant(grant.grantRevision, grant.serverId, origin.role_id,
            frozenset({'read_only'} if grant.risk == 'read_only' else {'read_only', 'mutating'} if grant.risk == 'mutating'
                else {'read_only', 'mutating', 'external'})) for grant in config.driftMcpGrants)
        self.capabilities = CapabilityRegistry(tuple(_LocalTool(self, item) for item in self.definitions.values())).resolve(
            role_id=origin.role_id, session_key=origin.task_origin().session_key, run_id=origin.tick_id,
            enabled_tools={'activate_skill', 'mount_server', 'finish_drift'} | set(config.driftToolGrants)
                | ({'meme_copy_image'} if config.driftMeme.enabled and config.driftMeme.copyEnabled else set()),
            allowed_risks={'read_only', 'mutating', 'external'}, discovery_enabled=True, service_grants=grants,
            authorize_tool=lambda definition, context: self.file_authorized(context))

    @contextmanager
    def transaction(self):
        with self.app._transaction() as db:
            if not self.app._authorized_in(db, self.origin):
                raise PermissionError('autonomous Drift generation was sealed')
            run = db.execute('SELECT mode FROM autonomy_runs WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            if run is None or run[0] != 'drift':
                raise PermissionError('the original tick did not select Drift')
            yield db

    def state(self):
        with self.transaction() as db:
            return dict(db.execute('SELECT * FROM autonomy_drift_turns WHERE tick_id=?', (self.origin.tick_id,)).fetchone())

    def reserve_request(self, payload_bytes, max_tokens):
        with self.transaction() as db:
            row = db.execute('SELECT * FROM autonomy_drift_turns WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            if (row['finish'] is not None or row['steps'] >= self.config.driftMaxSteps or row['tokens'] + max_tokens > 8192
                    or payload_bytes > 48 * 1024 or row['input_bytes'] + payload_bytes > 512 * 1024):
                raise RuntimeError('Drift model budget exhausted')
            db.execute('UPDATE autonomy_drift_turns SET steps=steps+1,tokens=tokens+?,input_bytes=input_bytes+? WHERE tick_id=?',
                (max_tokens, payload_bytes, self.origin.tick_id))

    def check_model_request(self):
        if self.meme_scope is not None:
            context = ToolContext(self.origin.role_id, self.origin.task_origin().session_key, self.origin.tick_id, self.signal)
            self.app.memes.validate_scope(self, context)

    async def reserve_image_request(self, usage):
        from ..ai.request_budget import ImageRequestBudgetError
        context = ToolContext(self.origin.role_id, self.origin.task_origin().session_key, self.origin.tick_id, self.signal)
        if usage.image_occurrences:
            try:
                self.app.memes.authorize(self, context, 'read_file', '')
            except (PermissionError, ValueError, OSError):
                raise ImageRequestBudgetError('authorization') from None
        elif (self.meme_scope is None or not self.file_authorized(context)
                or self.app.memes._grant(self.origin.role_id) != self.meme_scope['grant']
                or self.app.memes._root_identity(self.origin.role_id) != self.meme_scope['root']):
            raise ImageRequestBudgetError('authorization')
        with self.transaction() as db:
            row = db.execute('SELECT image_occurrences,image_base64_bytes,image_input_estimate,envelope_bytes FROM autonomy_drift_turns WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            for code, total, amount, maximum in (
                    ('run_image_occurrences', row[0], usage.image_occurrences, 4),
                    ('run_image_bytes', row[1], usage.image_base64_bytes, 4 * 1024 * 1024),
                    ('run_image_estimate', row[2], usage.image_input_estimate, 32768),
                    ('run_envelope_bytes', row[3], usage.envelope_bytes, 160 * 1024)):
                if total + amount > maximum:
                    raise ImageRequestBudgetError(code)
            db.execute('''UPDATE autonomy_drift_turns SET image_occurrences=image_occurrences+?,
                image_base64_bytes=image_base64_bytes+?,image_input_estimate=image_input_estimate+?,
                envelope_bytes=envelope_bytes+?,wire_body_bytes=wire_body_bytes+?,wire_requests=wire_requests+1 WHERE tick_id=?''',
                (usage.image_occurrences, usage.image_base64_bytes, usage.image_input_estimate, usage.envelope_bytes, usage.body_bytes, self.origin.tick_id))
            safe = {'protocol': usage.protocol, 'bodyBytes': usage.body_bytes, 'imageOccurrences': usage.image_occurrences,
                'imageBase64Bytes': usage.image_base64_bytes, 'imageInputEstimate': usage.image_input_estimate,
                'wireNonImageBytes': usage.wire_non_image_bytes, 'envelopeBytes': usage.envelope_bytes, 'bodySha256': usage.body_sha256}
            db.execute('INSERT INTO autonomy_meme_model_requests VALUES(?,?,?,?)',
                (uuid.uuid4().hex, self.origin.role_id, self.origin.tick_id, _json(safe)))
            retention.check_capacity(db, self.origin.role_id)

    def file_authorized(self, context):
        return ((context.role_id, context.session_key, context.run_id) ==
            (self.origin.role_id, self.origin.task_origin().session_key, self.origin.tick_id)
            and not self.signal.is_cancelled() and self.app._authorized(self.origin)
            and _directory_identity(self.root) == self.root_identity)

    async def close(self):
        self.signal.cancel()
        for process in tuple(self.processes):
            process.cancel()
        from ..mcp_stdio import finish_cleanup
        for pending in tuple(self.process_tasks):
            try:
                await finish_cleanup(pending)
            except Exception:
                if not pending.done():
                    raise
                # ProcessExecutor completes (including a pump failure) only
                # after its native process tree and reader threads are joined.
                pending.exception()
        self.process_tasks.clear()
        self.processes.clear()
        await self.capabilities.current().close()

    def schemas(self):
        schemas = self.capabilities.current().provider_schemas()
        from .meme_profile import is_profile
        if self.skill is None or not is_profile(self.skill) or not self.config.driftMeme.copyEnabled:
            schemas = tuple(item for item in schemas if item['function']['name'] != 'meme_copy_image')
        with self.transaction() as db:
            delivery = db.execute('SELECT status FROM autonomy_deliveries WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
        if delivery is not None and delivery[0] == 'sent':
            return tuple(item for item in schemas if cast(dict[str, object], item['function'])['name'] in {'write_file', 'edit_file', 'finish_drift'})
        return schemas

    async def activate(self, identifier):
        skill, manifest, digest = self.app.drift_skills.activate(self.origin.role_id, identifier)
        expected = next((item for item in self.catalog if item['id'] == identifier), None)
        if expected is None or expected.get('bundleSha256') != digest:
            raise PermissionError('Drift instruction bundle was not frozen in this run')
        for server in manifest.requiresMCP:
            if self.bundle is None:
                raise PermissionError('Drift MCP owner is unavailable')
            await self.bundle.prepare_mount(server.serverId)
        with self.transaction() as db:
            row = db.execute('SELECT active_skill,bundle_hash FROM autonomy_drift_turns WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            if row[0] is not None and (row[0], row[1]) != (identifier, digest):
                raise ValueError('this Drift run already activated its instruction bundle')
            db.execute('UPDATE autonomy_drift_turns SET active_skill=?,bundle_hash=? WHERE tick_id=?',
                (identifier, digest, self.origin.tick_id))
        self.skill, self.manifest, self.bundle_hash = skill, manifest, digest
        current = next(item for item in self.app.drift_skills.list(self.origin.role_id) if item['id'] == identifier)
        return {'skillId': identifier, 'bundleSha256': digest, 'instructions': skill.body, 'progress': current['progress']}

    async def dispatch(self, name, arguments, *, call_id=None):
        from .meme_profile import is_profile
        if is_profile(self.skill):
            context = ToolContext(self.origin.role_id, self.origin.task_origin().session_key, self.origin.tick_id, self.signal)
            self.app.memes.validate_scope(self, context)
        resolution = self.capabilities.current()
        definition = resolution.definition(name)
        if definition is None or name not in {item['function']['name'] for item in self.schemas()}:
            raise PermissionError('tool is not active in this Drift mode')
        ToolRegistry.validate_definition(definition, arguments)
        state = self.state()
        if state['finish'] is not None:
            if name == 'finish_drift':
                return self.finish(arguments)
            raise PermissionError('Drift already finalized')
        context = ToolContext(self.origin.role_id, self.origin.task_origin().session_key, self.origin.tick_id,
            self.signal, tool_call_id=call_id, capability_snapshot_id=resolution.snapshot.snapshot_id, capability_binding=resolution)
        if resolution.authorization_denial(definition, context, arguments) is not None:
            raise PermissionError('Drift tool authority was revoked')
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            raise ValueError('Drift provider call requires an immutable bounded ID')
        arguments_hash = hashlib.sha256(_json(arguments).encode()).hexdigest()
        with self.transaction() as db:
            prior = db.execute('SELECT * FROM autonomy_drift_tool_calls WHERE tick_id=? AND call_id=?', (self.origin.tick_id, call_id)).fetchone()
            if prior is not None:
                if (prior['tool_name'], prior['arguments_hash']) != (name, arguments_hash) or prior['result'] is None or prior['is_error']:
                    raise ValueError('Drift call identity conflicts or its original result is unresolved')
                return json.loads(prior['result'])
            binding = {**resolution.call_record(name), 'activeSkill': self.skill.skill_id if self.skill else None,
                'bundleHash': self.bundle_hash, 'authorHash': self.skill.content_hash if self.skill else None}
            db.execute('INSERT INTO autonomy_drift_tool_calls(tick_id,role_id,call_id,tool_name,arguments_hash,snapshot_id,binding_json) VALUES(?,?,?,?,?,?,?)',
                (self.origin.tick_id, self.origin.role_id, call_id, name, arguments_hash, resolution.snapshot.snapshot_id, _json(binding)))
            retention.check_capacity(db, self.origin.role_id)
        try:
            result = await self._dispatch(name, arguments, context, resolution)
            encoded = _json(result)
            if len(encoded.encode()) > 16000:
                raise ValueError('Drift tool result exceeds its bounded record')
        except (ValueError, PermissionError):
            with self.transaction() as db:
                db.execute('UPDATE autonomy_drift_tool_calls SET result=\'{}\',is_error=1 WHERE tick_id=? AND call_id=?', (self.origin.tick_id, call_id))
            raise
        with self.transaction() as db:
            db.execute('UPDATE autonomy_drift_tool_calls SET result=?,is_error=? WHERE tick_id=? AND call_id=?',
                (encoded, int(isinstance(result, dict) and result.get('isError') is True), self.origin.tick_id, call_id))
            retention.check_capacity(db, self.origin.role_id)
        return result

    async def _dispatch(self, name, arguments, context, resolution):
        if name == 'activate_skill':
            return await self.activate(arguments['skill_id'])
        if name == 'finish_drift':
            return self.finish(arguments)
        if name == 'message_push':
            from .drift_delivery import send_once
            return await send_once(self, arguments)
        if name == 'meme_copy_image':
            from .meme_copy import execute
            return await self.app._owned(execute, self.app.memes, self, arguments, context)
        if name == 'mount_server':
            if self.skill is None or self.manifest is None or self.bundle is None or not any(item.serverId == arguments['server_id'] for item in self.manifest.requiresMCP):
                raise PermissionError('MCP server is outside the active skill dependencies')
            server_id = arguments['server_id']
            declarations = await self.bundle.prepare_mount(server_id)
            config = next(item for item in self.config.driftMcpGrants if item.serverId == server_id)
            if self.bundle.is_mounted(server_id):
                return {'status': 'mounted', 'serverId': server_id}
            required = next(item for item in self.manifest.requiresMCP if item.serverId == server_id)
            names = {item.name for item in required.tools}
            tools = tuple(_RemoteTool(self, server_id, item, config.risk) for item in declarations if item['name'] in names)
            resolution.stage_service_tools(context, grant_id=config.grantRevision,
                parent_snapshot_id=resolution.snapshot.snapshot_id, tools=tools, activate=True)
            revision = self.capabilities.pending_revision(context.tool_call_id)
            if revision is None:
                raise RuntimeError('Drift mount did not produce an immutable revision')
            if not self.app._authorized(self.origin):
                raise PermissionError('Drift grant was sealed before capability publication')
            self.capabilities.publish_revision(revision)
            self.bundle.activate_mount(server_id)
            return {'status': 'mounted', 'serverId': server_id, 'tools': [item.definition.name for item in tools]}
        if name.startswith('mcp_'):
            permitted = {f'mcp_{server.serverId}__{operation.name}' for server in self.manifest.requiresMCP for operation in server.tools} if self.manifest else set()
            if self.skill is None or name not in self.skill.tools or name not in permitted:
                raise PermissionError('MCP tool is outside the active skill declaration')
            tool = resolution.get(name)
            result = await tool.execute(arguments, context)
            return {'content': result.content, 'details': result.details, 'isError': result.is_error}
        if self.skill is None or self.manifest is None or name not in self.skill.tools or not self.app.tool_permitted(self.origin, name):
            raise PermissionError('tool is outside the activated skill and frozen role grant')
        if name == 'shell':
            return await drift_shell.execute(self, arguments['rule_id'])
        if name == 'recall_memory':
            if self.app.recall_memory is None:
                raise PermissionError('Drift memory adapter is unavailable')
            return (await self.app.recall_memory(self.origin, arguments['query'], limit=2))[:2]
        if name in {'web_search', 'web_fetch'}:
            if self.app.web_tools is None:
                raise PermissionError('Drift webpage adapter is unavailable')
            return await self.app.web_tools(self.origin, name, arguments)
        if name in self.history_tools:
            result = await self.history_tools[name].execute(arguments, context)
            return {'content': result.content, 'isError': result.is_error}
        path = arguments.get('path')
        if isinstance(path, str) and (path == '@memes' or path.startswith('@memes/')):
            return await self.app.memes.execute(self, name, arguments, context)
        if (not isinstance(path, str) or not path or '\\' in path or ':' in path
                or PurePosixPath(path).is_absolute() or any(part in {'', '.', '..'} for part in path.split('/'))):
            raise PermissionError('Drift tools require a canonical relative path')
        if name in {'write_file', 'edit_file'} and PurePosixPath(path).parts[0].casefold() == 'state':
            raise PermissionError('Drift progress projections are host-owned')
        if path.startswith('@resource/'):
            identifier = path.removeprefix('@resource/')
            if (name != 'read_file' or identifier not in self.config.driftReadResourceIds
                    or identifier not in self.manifest.readResourceIds or identifier not in self.resources):
                raise PermissionError('Drift resource is outside the explicit read-only grants')
            lines = self.resources[identifier].splitlines()
            offset, limit = arguments.get('offset', 0), min(400, arguments.get('limit', 400))
            text = '\n'.join(lines[offset:offset + limit])
            return {'resourceId': identifier, 'content': text[:10000], 'truncated': len(text) > 10000 or len(lines) > offset + limit}
        tool = self.file_tools.get(name)
        if tool is None:
            raise PermissionError('Drift shared tool is unavailable')
        result = await tool.execute(arguments, context)
        if name in {'write_file', 'edit_file'} and not result.is_error:
            await self.app._owned(self.app.drift_skills.record_autonomous_write, self, context, path,
                result.details.get('publication') if isinstance(result.details, dict) else None)
        details = {key: value for key, value in result.details.items() if key != 'publication'} if isinstance(result.details, dict) else result.details
        return {'content': result.content, 'details': details, 'isError': result.is_error}

    def finish(self, arguments):
        if self.skill is None or arguments['skill_used'] != self.skill.skill_id:
            raise ValueError('finish requires the actual activated skill')
        if not arguments['one_line'].strip() or not arguments['next_action'].strip():
            raise ValueError('finish summary and next action must be nonempty')
        canonical = _json(arguments)
        with self.transaction() as db:
            prior = db.execute('SELECT finish,receipt FROM autonomy_drift_turns WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            if prior[0] is not None:
                if prior[0] != canonical:
                    raise ValueError('conflicting Drift finish')
                return json.loads(prior[1])
        # Revalidate the actual activated immutable instruction bundle; work
        # files and progress are separate and may legitimately have changed.
        _, _, digest = self.app.drift_skills.activate(self.origin.role_id, self.skill.skill_id)
        if digest != self.bundle_hash:
            raise PermissionError('Drift instruction bundle changed before finish')
        with self.transaction() as db:
            row = db.execute('SELECT finish,receipt FROM autonomy_drift_turns WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            if row[0] is not None:
                if row[0] != canonical:
                    raise ValueError('conflicting Drift finish')
                return json.loads(row[1])
            delivery = db.execute('SELECT * FROM autonomy_deliveries WHERE tick_id=?', (self.origin.tick_id,)).fetchone()
            from .drift_delivery import actual_outcome
            actual = actual_outcome(delivery)
            if arguments['message_result'] != actual:
                raise ValueError('finish outcome does not match the actual delivery claim and receipt')
            receipt = {'status': 'finalized', 'messageResult': actual,
                'receiptId': hashlib.sha256((self.origin.tick_id + canonical).encode()).hexdigest()}
            db.execute('UPDATE autonomy_drift_turns SET finish=?,receipt=? WHERE tick_id=?', (canonical, _json(receipt), self.origin.tick_id))
            now = self.app._now(db)
            db.execute('''INSERT INTO autonomy_drift_progress
                (role_id,skill_id,bundle_hash,run_count,last_run,one_line,next_action,message_result,projection_pending,root_identity,last_tick_id)
                VALUES(?,?,?,1,?,?,?,?,1,?,?)
                ON CONFLICT(role_id,skill_id) DO UPDATE SET bundle_hash=excluded.bundle_hash,
                run_count=run_count+1,last_run=excluded.last_run,one_line=excluded.one_line,
                next_action=excluded.next_action,message_result=excluded.message_result,projection_pending=1,
                root_identity=excluded.root_identity,last_tick_id=excluded.last_tick_id''',
                (self.origin.role_id, self.skill.skill_id, self.bundle_hash, now, arguments['one_line'], arguments['next_action'], actual,
                 str(self.root_identity), self.origin.tick_id))
            status = 'finished_' + actual if actual in {'sent', 'silent'} else 'finish_' + actual
            db.execute('UPDATE autonomy_runs SET status=? WHERE tick_id=?', (status, self.origin.tick_id))
            retention.check_capacity(db, self.origin.role_id)
        return receipt


def project_progress(app, role_id):
    with app._transaction() as db:
        rows = db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=? AND projection_pending=1', (role_id,)).fetchall()
    for row in rows:
        root = app.drift_skills.workspace(role_id) / 'drift'
        if str(_directory_identity(root)) != row['root_identity']:
            raise PermissionError('Drift root changed after committed progress')
        value = app.drift_skills.progress(row)
        value['projectionPending'] = False
        app.drift_files.project(role_id, row['skill_id'] + '.json', _json(value), expected_root_identity=row['root_identity'])
        with app._transaction() as db:
            if str(_directory_identity(root)) != row['root_identity']:
                raise PermissionError('Drift root changed during committed progress projection')
            db.execute('''UPDATE autonomy_drift_progress SET projection_pending=0
                WHERE role_id=? AND skill_id=? AND run_count=?''', (role_id, row['skill_id'], row['run_count']))


class _LocalTool:
    def __init__(self, turn, definition):
        self.turn, self.definition = turn, definition

    async def execute(self, arguments, context, on_update=None):
        value = await self.turn.dispatch(self.definition.name, arguments, call_id=context.tool_call_id)
        return AgentToolResult(_json(value))


class _RemoteTool:
    def __init__(self, turn, server_id, declaration, risk):
        from ..mcp_autonomy import declaration_hash
        self.turn, self.server_id, self.remote_name = turn, server_id, declaration['name']
        self.definition = ToolDefinition(f'mcp_{server_id}__{self.remote_name}', declaration.get('description', self.remote_name),
            declaration['inputSchema'], source=f'mcp:{server_id}', risk=risk, version=declaration_hash(declaration))

    async def execute(self, arguments, context, on_update=None):
        if not self.turn.file_authorized(context):
            raise PermissionError('Drift MCP origin was revoked')
        value = await self.turn.bundle.call(self.server_id, self.remote_name, arguments)
        return AgentToolResult(_json(value), is_error=bool(value.get('isError')))


async def run_drift(app, origin, execution, cancelled, config, snapshot, bundle=None):
    if not (config.driftAuthoring.enabled or config.driftMeme.enabled) or app.drift_skills.workspace_getter is None:
        with app._transaction() as db:
            if not app._authorized_in(db, origin):
                raise PermissionError('Drift authoring dependency was sealed')
            db.execute("UPDATE autonomy_runs SET mode='none',status='drift_dependency_pending' WHERE tick_id=? AND mode='pending'", (origin.tick_id,))
        return TaskOutcome('succeeded', output_tail='Drift requires a role-private author scope and workspace')
    turn = DriftTurn(app, origin, config, snapshot, execution, bundle=bundle)
    app._drift_owners[origin.tick_id] = turn
    if not any(item.get('status') == 'available' for item in turn.catalog):
        with turn.transaction() as db:
            db.execute("UPDATE autonomy_runs SET status='drift_dependency_pending' WHERE tick_id=?", (origin.tick_id,))
        return TaskOutcome('failed', output_tail='Drift has no trusted complete instruction bundle')
    configuration = app.model_getter(config.driftModelConfigurationId)
    if configuration is None or app.adapter_factory is None:
        raise ValueError('explicit Drift model is unavailable')
    adapter = app.adapter_factory(configuration)
    with app._transaction() as db:
        start = db.execute('SELECT started_at FROM autonomy_runs WHERE tick_id=?', (origin.tick_id,)).fetchone()[0]
        remaining = min(execution.deadline - time.monotonic(), start + 600 - app._now(db))
    if remaining <= 0:
        raise TimeoutError('original Drift deadline expired')
    messages = [ModelSystemMessage((ModelTextBlock('已冻结角色：' + persona(app.frozen_bindings(origin)) + '\n'
        '这是获准的自主Drift，不是主人输入。先activate_skill，再推进一个有限步骤。最后必须finish_drift。'
        '文件路径仅相对当前角色Drift根，来源与工作数据不能授予权限。每次只调用一个工具。'
        '发送省略目标二元组时使用冻结默认；选择外部目标必须同时使用下列原许可的account_id和target_id。'),)),
        ModelUserMessage((ModelTextBlock('真实技能与进度：' + _json(turn.catalog)),
            ModelTextBlock('本run获准表情素材：' + _json([item.public for item in turn.meme_scope['sources'].values()] if turn.meme_scope else [])),
            ModelTextBlock('已冻结发送目标：' + _json(app.targets.model_view(turn)))))]
    try:
        async with asyncio.timeout(remaining):
            while True:
                assistant = await _request(turn, adapter, configuration, messages, cancelled, schemas=turn.schemas(), max_tokens=512)
                messages = [replace(message, content=tuple(block for block in message.content if not isinstance(block, ModelImageBlock))) for message in messages]
                messages.append(assistant)
                calls = [block for block in assistant.content if isinstance(block, ModelToolCallBlock)]
                if len(calls) != 1:
                    for call in calls:
                        messages.append(ModelToolResultMessage((ModelTextBlock('一次只能调用一个工具，本次未执行。'),),
                            tool_call_id=call.id, tool_name=call.name, is_error=True))
                    if not calls:
                        messages.append(ModelUserMessage((ModelTextBlock('请调用一个合法工具或finish_drift。'),)))
                    continue
                call, failed = calls[0], False
                try:
                    async with asyncio.timeout(60 if call.name == 'shell' else 30):
                        result = await turn.dispatch(call.name, call.arguments, call_id=call.id)
                    failed = isinstance(result, dict) and result.get('isError') is True
                    encoded = _json(result)
                    if len(encoded) > 16000:
                        raise ValueError('Drift tool result exceeds budget')
                except (PermissionError, ValueError):
                    encoded, failed = _json({'error': 'Drift tool, arguments, scope, or result budget rejected'}), True
                messages.append(ModelToolResultMessage((ModelTextBlock(encoded),), tool_call_id=call.id, tool_name=call.name, is_error=failed))
                if turn.pending_images:
                    messages.append(ModelUserMessage(tuple(ModelImageBlock(image.media_type, image.data_base64, image.detail) for image in turn.pending_images)))
                    turn.pending_images = ()
                state = turn.state()
                if state['finish'] is not None:
                    try:
                        await app._owned(project_progress, app, origin.role_id)
                    except (ValueError, OSError):
                        pass  # SQLite finish is authoritative; recovery owns projection.
                    outcome = json.loads(state['receipt'])['messageResult']
                    return TaskOutcome('succeeded' if outcome in {'sent', 'silent'} else 'failed', output_tail='Drift finish ' + outcome)
    finally:
        turn.signal.cancel()
