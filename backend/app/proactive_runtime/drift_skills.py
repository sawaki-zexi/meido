"""Host-provenanced Drift instruction bundles; authoring grants never execute tools."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..agent_runtime.skills import SkillRegistry
from .config import AutonomyConfig, DriftAuthoringGrant
from .source_authoring import _directory_identity
from .source_validation import _read, _unique_object
from . import retention
from .drift_workspace import _identity


BUILTIN_ROOT = Path(__file__).resolve().parents[1] / 'builtin_skills'
_ID = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
_INTERNAL = frozenset({'finish_drift', 'activate_skill', 'mount_server'})
_BUILTINS = frozenset({'create-drift-skill', 'meme-manage'})


class RequiredOperation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    name: str = Field(min_length=1, max_length=64)
    definitionHash: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class RequiredServer(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    serverId: str = Field(pattern=r'^[a-z][a-z0-9_]{0,23}$')
    tools: list[RequiredOperation] = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def unique_tools(self):
        if len({item.name for item in self.tools}) != len(self.tools):
            raise ValueError('duplicate required MCP operation')
        return self


class DriftManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    profile: str = Field(pattern=r'^meido-drift-skill-v1$')
    version: int = Field(ge=1, le=1)
    skillId: str = Field(pattern=r'^[a-z0-9][a-z0-9._-]{0,63}$')
    requiresMCP: list[RequiredServer] = Field(default_factory=list, max_length=8)
    readResourceIds: list[str] = Field(default_factory=list, max_length=16)
    shellRuleIds: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def finite_dependencies(self):
        if len({server.serverId for server in self.requiresMCP}) != len(self.requiresMCP):
            raise ValueError('duplicate required MCP server')
        for values in (self.readResourceIds, self.shellRuleIds):
            if len(set(values)) != len(values) or any(not value or len(value) > 128 for value in values):
                raise ValueError('invalid required resource or Shell rule')
        return self


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def authoring_revision(requested, previous):
    """Only the owner configuration transaction allocates a source identity."""
    if requested.scopeId and requested.scopeId != previous.scopeId:
        raise ValueError('author scope identity must be allocated by the host')
    fields = {'scopeId', 'revision'}
    unchanged = requested.model_dump(exclude=fields) == previous.model_dump(exclude=fields)
    if unchanged:
        return previous
    import uuid
    identifier = previous.scopeId
    if requested.enabled and not previous.enabled:
        identifier = uuid.uuid4().hex
    return requested.model_copy(update={'scopeId': identifier, 'revision': previous.revision + 1})


class DriftSkillCatalog:
    def __init__(self, app, workspace_getter=None):
        self.app, self.workspace_getter = app, workspace_getter
        with app._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_authored_files (
                role_id TEXT NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,bytes INTEGER NOT NULL,
                scope_id TEXT NOT NULL,scope_revision INTEGER NOT NULL,run_id TEXT NOT NULL,call_id TEXT NOT NULL,
                author_skill_hash TEXT NOT NULL,writer_snapshot TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(role_id,path,digest,scope_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_progress (
                role_id TEXT NOT NULL,skill_id TEXT NOT NULL,bundle_hash TEXT NOT NULL,run_count INTEGER NOT NULL,
                last_run REAL NOT NULL,one_line TEXT NOT NULL,next_action TEXT NOT NULL,message_result TEXT NOT NULL,
                projection_pending INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(role_id,skill_id))''')
            progress_columns = {row[1] for row in db.execute('PRAGMA table_info(autonomy_drift_progress)')}
            for column in ('root_identity', 'last_tick_id'):
                if column not in progress_columns:
                    db.execute('ALTER TABLE autonomy_drift_progress ADD COLUMN ' + column + " TEXT NOT NULL DEFAULT ''")
            columns = {row[1] for row in db.execute('PRAGMA table_info(autonomy_drift_authored_files)')}
            if 'identity' not in columns:
                db.execute("ALTER TABLE autonomy_drift_authored_files ADD COLUMN identity TEXT NOT NULL DEFAULT ''")
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_previews (
                preview_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,skill_id TEXT NOT NULL,bundle_hash TEXT NOT NULL,
                scope_id TEXT NOT NULL,scope_revision INTEGER NOT NULL,config_revision INTEGER NOT NULL,
                identity TEXT NOT NULL,files TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0)''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_adoptions (
                role_id TEXT NOT NULL,skill_id TEXT NOT NULL,bundle_hash TEXT NOT NULL,scope_id TEXT NOT NULL,
                scope_revision INTEGER NOT NULL,preview_id TEXT NOT NULL,identity TEXT NOT NULL,
                PRIMARY KEY(role_id,skill_id,bundle_hash,scope_id,scope_revision))''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_drift_logs (
                tick_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,bytes INTEGER NOT NULL)''')

    def configuration(self, role_id):
        self.app._require_role(role_id)
        with self.app._transaction() as db:
            row = db.execute('SELECT config FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()
            return AutonomyConfig.model_validate_json(row[0]) if row else AutonomyConfig()

    def workspace(self, role_id):
        self.app._require_role(role_id)
        if self.workspace_getter is None:
            raise ValueError('host Drift workspace is unavailable')
        return Path(self.workspace_getter(role_id)).absolute()

    def author_tools(self, tools, role_id, *, authorized):
        scope = self.configuration(role_id).driftAuthoring
        tools = self.app.memes.owner_tools(tools, role_id, authorized)
        return tuple(_AuthorTool(self, tool.with_write_policy(self.app.drift_files.policy(role_id)), role_id, scope, authorized)
            if tool.definition.name in {'write_file', 'edit_file'} else tool for tool in tools)

    def _writer(self, role_id, scope, context, authorized):
        current = self.configuration(role_id).driftAuthoring
        if not scope.enabled or current != scope or not authorized(context):
            return None
        run = self.app.sessions.get_run(context.run_id)
        user = self.app.sessions.user_message_for_run(context.run_id)
        if (run is None or user is None or run.roleId != role_id or run.sessionKey != context.session_key
                or run.status not in {'created', 'running'} or user.metadata.get('internalEvent')
                or user.role != 'user' or user.messageType != 'text' or user.status != 'completed'):
            return None
        snapshot = self.app.sessions.active_call_binding(context.run_id, context.tool_call_id,
            tool_names={'write_file', 'edit_file'})
        if snapshot is None or snapshot.get('snapshotId') != context.capability_snapshot_id:
            return None
        author = next((item for item in snapshot.get('skills', []) if item.get('id') == 'create-drift-skill'), None)
        expected = next((skill for skill in SkillRegistry.discover(BUILTIN_ROOT, source='builtin').skills
            if skill.skill_id == 'create-drift-skill'), None)
        if author is None or author.get('source') != 'builtin' or expected is None:
            return None
        expected = expected.materialize()
        if author.get('contentHash') != expected.content_hash:
            return None
        provenance = {'format': 'drift-author-v1', 'kind': 'owner', 'roleId': role_id,
            'runId': context.run_id, 'callId': context.tool_call_id, 'requestId': context.capability_snapshot_id,
            'author': {'id': author['id'], 'source': author['source'], 'contentHash': author['contentHash']},
            'scopeId': scope.scopeId, 'scopeRevision': scope.revision}
        return author, provenance

    def record_write(self, role_id, scope, context, authorized, path, publication):
        evidence = self._writer(role_id, scope, context, authorized)
        if evidence is None:
            return
        relative = PurePosixPath(path)
        if relative.is_absolute() or '..' in relative.parts or relative.parts[:2] != ('drift', 'skills'):
            return
        workspace = self.workspace(role_id)
        identity = _directory_identity(workspace)
        raw = _read(workspace.joinpath(*relative.parts), 8 * 1024 * 1024)
        if publication != {'sha256': _digest(raw), 'identity': list(_identity(workspace.joinpath(*relative.parts)))}:
            raise PermissionError('S06 file changed after the actual owned write')
        author, snapshot = evidence
        if _directory_identity(workspace) != identity or self._writer(role_id, scope, context, authorized) is None:
            raise PermissionError('author scope changed during file publication')
        with self.app._transaction() as db:
            live = db.execute('SELECT config FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()
            if live is None or AutonomyConfig.model_validate_json(live[0]).driftAuthoring != scope:
                raise PermissionError('author scope was revoked')
            db.execute('''INSERT OR REPLACE INTO autonomy_drift_authored_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (role_id, relative.as_posix(), _digest(raw), len(raw), scope.scopeId, scope.revision,
                 context.run_id, context.tool_call_id, author['contentHash'], _json(snapshot), self.app._now(db),
                 self._identity_snapshot(workspace, [(relative.as_posix(), _digest(raw), len(raw))])))
            retention.check_capacity(db, role_id)

    def _bundle(self, role_id, skill, root):
        root_identity = _directory_identity(root)
        skill_path = root.joinpath(*PurePosixPath(skill.path).parts)
        directory_identity = _directory_identity(skill_path.parent)
        raw = _read(skill_path, 256 * 1024)
        materialized = skill.materialize()
        if len(materialized.body.encode('utf-8')) > 12000:
            raise ValueError('Drift instruction body exceeds its complete activation budget')
        resources, sidecar = [], None
        expected = (PurePosixPath(skill.path).parent / 'references/drift.json').as_posix()
        for resource in skill.supporting_files:
            value = _read(root.joinpath(*PurePosixPath(resource.path).parts), 256 * 1024)
            if len(value) != resource.size or _digest(value) != resource.content_hash:
                raise ValueError('Drift support resource changed after materialization')
            resources.append((resource.path, resource.content_hash, resource.size))
            if resource.path == expected:
                sidecar = DriftManifest.model_validate(json.loads(value, object_pairs_hook=_unique_object))
        if skill.source == 'builtin' and skill.skill_id in _BUILTINS:
            sidecar = DriftManifest(profile='meido-drift-skill-v1', version=1, skillId=skill.skill_id)
        if sidecar is None or sidecar.skillId != materialized.skill_id:
            raise ValueError('Drift skill requires its matching references/drift.json')
        if _digest(raw) != materialized.content_hash or (_directory_identity(root), _directory_identity(skill_path.parent)) != (root_identity, directory_identity):
            raise ValueError('Drift artifact identity changed')
        files = [(skill.path, materialized.content_hash, len(raw)), *resources]
        digest = _digest(_json(files).encode())
        from .meme_profile import effective
        materialized = effective(materialized, root)
        return materialized, sidecar, files, digest

    @staticmethod
    def _identity_snapshot(root, files):
        directories = {root: _directory_identity(root)}
        identities = []
        for name, sha, size in files:
            path = root.joinpath(*PurePosixPath(name).parts)
            parent = path.parent
            while parent != root:
                if not parent.is_relative_to(root):
                    raise PermissionError('artifact identity is outside its fixed root')
                directories[parent] = _directory_identity(parent)
                parent = parent.parent
            identities.append((name, _identity(path), sha, size))
        return _json({'directories': sorted((str(path), value) for path, value in directories.items()), 'files': identities})

    def _read_skill(self, role_id, identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError('invalid Drift skill ID')
        root = self.workspace(role_id) / 'drift/skills'
        _directory_identity(root)
        registry = SkillRegistry.discover(root, source='role')
        descriptor = next((item for item in registry.skills if item.skill_id == identifier), None)
        if descriptor is None:
            raise ValueError('Drift instruction bundle does not exist')
        return root, self._bundle(role_id, descriptor, root)

    def preview(self, role_id, identifier):
        config = self.configuration(role_id)
        root, (skill, manifest, files, digest) = self._read_skill(role_id, identifier)
        identity = self._identity_snapshot(root, files)
        preview_id = uuid.uuid4().hex
        with self.app._transaction() as db:
            live = db.execute('SELECT config FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()
            if live is None or AutonomyConfig.model_validate_json(live[0]) != config:
                raise ValueError('Drift scope changed while previewing')
            db.execute('INSERT INTO autonomy_drift_previews VALUES(?,?,?,?,?,?,?,?,?,0)',
                (preview_id, role_id, identifier, digest, config.driftAuthoring.scopeId,
                 config.driftAuthoring.revision, config.revision, identity, _json(files)))
            retention.check_capacity(db, role_id)
        current = next(item for item in self.list(role_id) if item['id'] == identifier)
        return {'previewId': preview_id, 'skillId': identifier, 'bundleSha256': digest,
            'files': [{'path': name, 'size': size, 'sha256': sha} for name, sha, size in files],
            'declaration': {'tools': list(skill.tools), **manifest.model_dump()},
            'scopeId': config.driftAuthoring.scopeId, 'scopeRevision': config.driftAuthoring.revision,
            'configRevision': config.revision, 'status': current['status']}

    def adopt(self, role_id, request):
        root, (skill, manifest, files, digest) = self._read_skill(role_id, request.skillId)
        config = self.configuration(role_id)
        scope = config.driftAuthoring
        if (not scope.enabled or scope.scopeId != request.authoringScopeId or scope.revision != request.expectedScopeRevision
                or config.revision != request.expectedRevision or digest != request.bundleSha256
                or self._forbidden(skill, manifest, scope)):
            raise ValueError('Drift adoption no longer matches the owner scope and exact bundle')
        identity = self._identity_snapshot(root, files)
        with self.app._transaction() as db:
            live = db.execute('SELECT config FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()
            preview = db.execute('SELECT * FROM autonomy_drift_previews WHERE preview_id=? AND role_id=?',
                (request.previewId, role_id)).fetchone()
            if (live is None or AutonomyConfig.model_validate_json(live[0]) != config or preview is None
                    or (preview['skill_id'], preview['bundle_hash'], preview['scope_id'], preview['scope_revision'], preview['config_revision'])
                    != (request.skillId, digest, scope.scopeId, scope.revision, config.revision)
                    or preview['identity'] != identity or preview['files'] != _json(files)
                    or self._identity_snapshot(root, files) != identity):
                raise ValueError('Drift preview was superseded')
            db.execute('INSERT OR IGNORE INTO autonomy_drift_adoptions VALUES(?,?,?,?,?,?,?)',
                (role_id, skill.skill_id, digest, scope.scopeId, scope.revision, request.previewId, identity))
            db.execute('UPDATE autonomy_drift_previews SET used=1 WHERE preview_id=?', (request.previewId,))
            retention.check_capacity(db, role_id)
        return {'skills': self.list(role_id)}

    @staticmethod
    def _forbidden(skill, manifest, scope):
        allowed = set(scope.allowedTools) | _INTERNAL
        from .meme_profile import is_profile
        if is_profile(skill):
            allowed.add('meme_copy_image')
        declared_mcp = {f'mcp_{server.serverId}__{tool.name}' for server in manifest.requiresMCP for tool in server.tools}
        return ([name for name in skill.tools if name not in allowed and not (name.startswith('mcp_') and scope.allowMcp)]
            + (['MCP declaration'] if manifest.requiresMCP and not scope.allowMcp else [])
            + (['Shell declaration'] if ('shell' in skill.tools or manifest.shellRuleIds) and not scope.allowShell else [])
            + (['resource declaration'] if set(manifest.readResourceIds) - set(scope.allowedResourceIds) else [])
            + (['MCP operation declaration mismatch'] if
                declared_mcp != {name for name in skill.tools if name.startswith('mcp_')} else []))

    def list(self, role_id):
        config = self.configuration(role_id)
        try:
            root = self.workspace(role_id) / 'drift/skills'
            if root.exists():
                _directory_identity(root)
                from .drift_workspace import _scan
                _scan(root)
            registry = self._registry(role_id)
        except (ValueError, OSError):
            return []
        result = []
        for descriptor in registry.skills[:256]:
            try:
                selected_root = BUILTIN_ROOT if descriptor.source == 'builtin' else root
                skill, manifest, files, digest = self._bundle(role_id, descriptor, selected_root)
                scope = config.driftAuthoring
                with self.app._transaction() as db:
                    evidence = [db.execute('''SELECT * FROM autonomy_drift_authored_files
                        WHERE role_id=? AND path=? AND digest=? AND scope_id=?''',
                        (role_id, 'drift/skills/' + name, sha, scope.scopeId)).fetchone() for name, sha, _ in files]
                    progress = db.execute('SELECT * FROM autonomy_drift_progress WHERE role_id=? AND skill_id=?',
                        (role_id, skill.skill_id)).fetchone()
                    adoption = db.execute('''SELECT * FROM autonomy_drift_adoptions WHERE role_id=? AND skill_id=?
                        AND bundle_hash=? AND scope_id=? AND scope_revision=?''',
                        (role_id, skill.skill_id, digest, scope.scopeId, scope.revision)).fetchone()
                authored = descriptor.source == 'role' and all(item is not None and item['scope_revision'] == scope.revision
                    and item['identity'] == self._identity_snapshot(self.workspace(role_id), [('drift/skills/' + name, sha, size)])
                    for item, (name, sha, size) in zip(evidence, files))
                adopted = adoption is not None and adoption['identity'] == self._identity_snapshot(root, files)
                builtin = descriptor.source == 'builtin'
                verified = scope.enabled and (authored or adopted or builtin)
                forbidden = self._forbidden(skill, manifest, scope)
                missing = sorted(set(skill.tools) - self.execution_tools(config) - _INTERNAL)
                role = self.app.role_getter(role_id)
                from .bindings import shape, local_tool_names
                local = local_tool_names(shape(role, role=True))
                from .meme_profile import is_profile
                if is_profile(skill):
                    # The finite maintenance purpose authorizes this fixed
                    # builtin. It grants no S06 or custom-skill authorship.
                    verified = config.driftMeme.enabled
                    forbidden = [name for name in skill.tools if name not in _INTERNAL | {'read_file', 'list_dir', 'write_file', 'edit_file', 'meme_copy_image'}]
                    local.add('meme_copy_image')
                    missing = [name for name in missing if name != 'meme_copy_image']
                missing = sorted(set(missing) | {name for name in skill.tools if name not in _INTERNAL and not name.startswith('mcp_') and name not in local})
                grants = {grant.serverId: grant for grant in config.driftMcpGrants}
                missing_mcp = []
                for server in manifest.requiresMCP:
                    grant = grants.get(server.serverId)
                    if grant is not None and not self.app.drift_permissions_current(role_id, (grant,)):
                        grant = None
                    operations = {item.name: item.definitionHash for item in grant.operations} if grant else {}
                    for operation in server.tools:
                        if operation.name not in operations or operation.definitionHash not in {None, operations.get(operation.name)}:
                            missing_mcp.append(server.serverId + ':' + operation.name)
                missing_resources = sorted(set(manifest.readResourceIds) - set(config.driftReadResourceIds))
                missing_shell = sorted(set(manifest.shellRuleIds) - set(config.shellRuleIds))
                if 'shell' in skill.tools or manifest.shellRuleIds:
                    from .drift_shell import presets, available
                    rules = presets()
                    if 'shell' in skill.tools and not manifest.shellRuleIds:
                        missing_shell.append('skill-readonly-preset')
                    if not config.shellRuleIds:
                        missing_shell.append('host-readonly-preset')
                    missing_shell.extend(identifier for identifier in manifest.shellRuleIds if identifier not in rules or not available(rules[identifier], shape(role, role=True)))
                    missing_shell = sorted(set(missing_shell))
                source = evidence[0] if authored and verified else None
                reason = ('尚未明确启用本角色 Drift 表情库图片核验与视觉维护用途；请配置同一 Drift 视觉模型、库读与类别许可；普通主人聊天仍可使用原表情技能。'
                    if builtin and skill.skill_id == 'meme-manage' and not config.driftMeme.enabled else
                    '尚未允许 Drift 自主作者；可在主人聊天使用本技能。'
                    if builtin and skill.skill_id == 'create-drift-skill' and not scope.allowAutonomousAuthoring else None)
                result.append({'id': skill.skill_id, 'version': skill.version, 'description': skill.description,
                    'bundleSha256': digest, 'tools': list(skill.tools), 'requiresMCP': manifest.model_dump()['requiresMCP'],
                    'status': 'pending_dependency' if reason else 'untrusted' if not verified or forbidden else 'pending_dependency' if missing or missing_mcp or missing_resources or missing_shell else 'available',
                    **({'reason': reason} if reason else {}),
                    'missingTools': missing, 'missingMcp': missing_mcp, 'missingResources': missing_resources, 'missingShellRules': missing_shell,
                    'provenance': {'status': 'verified' if verified else 'unverified',
                        'runId': source['run_id'] if source else None,
                        'authorSkillHash': source['author_skill_hash'] if source else skill.content_hash if builtin else None},
                    'progress': self.progress(progress) if progress else None})
            except (ValueError, OSError):
                result.append({'id': descriptor.skill_id, 'status': 'invalid', 'reason': 'artifact validation failed'})
        return result

    def _registry(self, role_id):
        registry = SkillRegistry.discover_many(((self.workspace(role_id) / 'drift/skills', 'role'), (BUILTIN_ROOT, 'builtin')))
        return SkillRegistry(skill for skill in registry.skills if skill.source == 'role' or skill.skill_id in _BUILTINS)

    def record_autonomous_write(self, turn, context, path, publication):
        scope = turn.config.driftAuthoring
        relative = PurePosixPath(path)
        if (not scope.allowAutonomousAuthoring or turn.skill is None or turn.skill.skill_id != 'create-drift-skill'
                or turn.skill.source != 'builtin' or relative.parts[:1] != ('skills',)):
            return
        if not turn.file_authorized(context):
            raise PermissionError('autonomous S06 origin was sealed')
        workspace = self.workspace(turn.origin.role_id)
        name = 'drift/' + relative.as_posix()
        raw = _read(workspace.joinpath(*PurePosixPath(name).parts), 256 * 1024)
        if publication != {'sha256': _digest(raw), 'identity': list(_identity(workspace.joinpath(*PurePosixPath(name).parts)))}:
            raise PermissionError('autonomous S06 file changed after its owned write')
        identity = self._identity_snapshot(workspace, [(name, _digest(raw), len(raw))])
        with self.app._transaction() as db:
            run = db.execute('SELECT active_skill,bundle_hash FROM autonomy_drift_turns WHERE tick_id=?', (turn.origin.tick_id,)).fetchone()
            call = db.execute('SELECT * FROM autonomy_drift_tool_calls WHERE tick_id=? AND call_id=?',
                (turn.origin.tick_id, context.tool_call_id)).fetchone()
            if (not self.app._authorized_in(db, turn.origin) or run is None or tuple(run) != ('create-drift-skill', turn.bundle_hash)
                    or call is None or call['tool_name'] not in {'write_file', 'edit_file'}
                    or call['snapshot_id'] != context.capability_snapshot_id or call['result'] is not None):
                raise PermissionError('autonomous S06 has no actual active-skill and call provenance')
            expected = next((skill.materialize() for skill in SkillRegistry.discover(BUILTIN_ROOT, source='builtin').skills
                if skill.skill_id == 'create-drift-skill'), None)
            binding = json.loads(call['binding_json'] or '{}')
            if (expected is None or turn.skill.content_hash != expected.content_hash
                    or binding.get('activeSkill') != 'create-drift-skill'
                    or binding.get('bundleHash') != turn.bundle_hash
                    or binding.get('authorHash') != expected.content_hash):
                raise PermissionError('autonomous S06 author definition changed')
            provenance = {'format': 'drift-author-v1', 'kind': 'autonomy', 'roleId': turn.origin.role_id,
                'runId': turn.origin.tick_id, 'callId': context.tool_call_id,
                'requestId': context.capability_snapshot_id, 'bundleHash': turn.bundle_hash,
                'author': {'id': 'create-drift-skill', 'source': 'builtin', 'contentHash': expected.content_hash},
                'scopeId': scope.scopeId, 'scopeRevision': scope.revision,
                'grantRevision': turn.origin.grant_revision}
            db.execute('INSERT OR REPLACE INTO autonomy_drift_authored_files VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (turn.origin.role_id, name, _digest(raw), len(raw), scope.scopeId, scope.revision,
                 turn.origin.tick_id, context.tool_call_id, turn.skill.content_hash, _json(provenance), self.app._now(db), identity))
            retention.check_capacity(db, turn.origin.role_id)

    @staticmethod
    def execution_tools(config):
        return set(config.driftToolGrants) | {f'mcp_{grant.serverId}__{operation.name}'
            for grant in config.driftMcpGrants for operation in grant.operations}

    @staticmethod
    def progress(row):
        return {'runCount': row['run_count'], 'lastRun': row['last_run'], 'oneLine': row['one_line'],
            'nextAction': row['next_action'], 'messageResult': row['message_result'],
            'projectionPending': bool(row['projection_pending'])}

    def activate(self, role_id, identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError('invalid Drift skill ID')
        current = next((item for item in self.list(role_id) if item['id'] == identifier), None)
        if current is None or current['status'] != 'available':
            raise PermissionError('Drift skill is untrusted or lacks dependencies')
        registry = self._registry(role_id)
        if identifier == 'meme-manage':
            descriptor = next((item for item in registry.skills if item.skill_id == identifier), None)
            if descriptor is not None and descriptor.source == 'builtin':
                skill, manifest, _, digest = self._bundle(role_id, descriptor, BUILTIN_ROOT)
                if digest != current['bundleSha256']:
                    raise PermissionError('builtin meme bundle changed before activation')
                return skill, manifest, digest
        resolution = registry.resolve(prompt='', explicit_ids={identifier},
            available_tools=self.execution_tools(self.configuration(role_id)) | _INTERNAL,
            enabled_tools=self.execution_tools(self.configuration(role_id)) | _INTERNAL)
        if len(resolution.skills) != 1:
            raise PermissionError('Drift skill changed before activation')
        skill = resolution.skills[0]
        root = BUILTIN_ROOT if skill.source == 'builtin' else self.workspace(role_id) / 'drift/skills'
        _, manifest, _, digest = self._bundle(role_id, skill, root)
        if digest != current['bundleSha256']:
            raise PermissionError('Drift instruction bundle changed before activation')
        return skill, manifest, digest


class _AuthorTool:
    def __init__(self, catalog, tool, role_id, scope, authorized):
        self.catalog, self.tool, self.role_id, self.scope, self.authorized = catalog, tool, role_id, scope, authorized
        self.definition = tool.definition

    async def execute(self, arguments, context, on_update=None):
        result = await self.tool.execute(arguments, context, on_update)
        if not result.is_error and isinstance(result.details, dict):
            try:
                self.catalog.record_write(self.role_id, self.scope, context, self.authorized,
                    result.details.get('path', ''), result.details.get('publication'))
            except (ValueError, OSError, PermissionError, sqlite3.Error):
                result = replace(result, details={**result.details, 'driftAuthoring': 'unverified; refresh artifact status'})
            result = replace(result, details={key: value for key, value in result.details.items() if key != 'publication'})
        return result
