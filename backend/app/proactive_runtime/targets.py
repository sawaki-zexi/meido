"""Owner-observed target grants; configuration and run authority remain distinct."""
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from ..channels.directory import DirectoryQuery


class TargetConflict(ValueError):
    pass


class DirectoryRevision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    account: int = Field(ge=0)
    platform: int = Field(ge=0)


class TargetSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    accountId: str = Field(min_length=1, max_length=256)
    targetId: str = Field(min_length=1, max_length=256)
    selectionObservationId: str = Field(pattern=r'^[a-f0-9]{32}$')
    expectedDirectoryRevision: DirectoryRevision


class TargetDirectoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    accountId: str = Field(min_length=1, max_length=256)
    kind: str = 'known'
    groupId: str = Field(default='', max_length=512)
    memberId: str = Field(default='', max_length=512)
    limit: int = Field(default=25, ge=1, le=50)
    cursor: str = Field(default='', max_length=512)

    def query(self):
        return DirectoryQuery(self.kind, self.groupId, self.memberId, self.limit, self.cursor)


def target_ref(role_id, account_id, target_id):
    value = json.dumps([role_id, account_id, target_id], separators=(',', ':'))
    return 'channel:' + hashlib.sha256(value.encode()).hexdigest()


def _grant(snapshot):
    # New runs freeze their current adapter; a process-local adapter identity is
    # not a durable grant and can never silently rebind the owner incarnation.
    return {key: value for key, value in snapshot.items()
        if key not in {'adapterGeneration', 'displayName', 'accountDisplayName', 'viaAccount'}}


class TargetGrants:
    def __init__(self, app, host):
        self.app, self.host = app, host
        self._observations = {}
        self._lock = threading.RLock()
        with app._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_target_grants (
                role_id TEXT NOT NULL,ref TEXT NOT NULL,snapshot TEXT NOT NULL,
                PRIMARY KEY(role_id,ref))''')
            db.execute('''CREATE TABLE IF NOT EXISTS autonomy_target_runs (
                tick_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,targets TEXT NOT NULL)''')

    def _shared_database(self):
        if (self.host is None or Path(self.host.store.database_path).resolve() != Path(self.app.sessions.database_path).resolve()
                or Path(self.host.store.sessions.database_path).resolve() != Path(self.app.sessions.database_path).resolve()):
            raise PermissionError('autonomous channels require the original shared session database')

    def accounts(self, role_id):
        self.app._require_role(role_id)
        return {'accounts': self.host.account_list(role_id) if self.host is not None else []}

    def _revision(self, role_id):
        # Directory persistence calls authorization inside its own write
        # transaction. This check observes configuration; it must not acquire a
        # second write lock or mutate the business clock.
        with sqlite3.connect(self.app.sessions.database_path, timeout=.05) as db:
            row = db.execute('SELECT revision FROM autonomy_roles WHERE role_id=?', (role_id,)).fetchone()
            return row[0] if row else 0

    async def directory(self, role_id, request):
        self.app._require_role(role_id)
        self._shared_database()
        query = request.query()
        revision = self._revision(role_id)
        def allowed():
            return self.app.role_getter(role_id) is not None and self._revision(role_id) == revision
        result = await self.host.query_directory(role_id, request.accountId, query, scope='autonomy-owner-selection', authorized=allowed)
        account = self.host.store.get_account(request.accountId)
        if account is None or account.role_id != role_id or not allowed():
            raise TargetConflict('target configuration changed during directory observation')
        pair = self.host.store.directory_revision(account.account_id, account.channel)
        result['directoryRevision'] = {'account': pair[0], 'platform': pair[1]}
        now = time.monotonic()
        with self._lock:
            self._observations = {key: value for key, value in self._observations.items() if value['expires'] > now}
            for item in result['targets']:
                item.update(targetRef=target_ref(role_id, account.account_id, item['id']), ownerEligible=False,
                    reason='仅当前账号已配对主人的私聊可选', selectionObservationId=None)
                if item['kind'] != 'private' or item.get('canSend') is False:
                    continue
                try:
                    snapshot = self.host.freeze_owner_target(role_id, account.account_id, item['id'])
                except PermissionError:
                    continue
                if len(self._observations) >= 512 or sum(value['role'] == role_id for value in self._observations.values()) >= 100:
                    item['reason'] = '目标观察已满；请稍后重新读取'
                    continue
                token = uuid.uuid4().hex
                self._observations[token] = {'role': role_id, 'revision': revision, 'query': query.identity(),
                    'cursor': query.cursor, 'snapshot': snapshot, 'expires': now + 60}
                item.update(ownerEligible=True, reason='', selectionObservationId=token)
        return result

    def save(self, db, role_id, config, expected_revision, selections):
        refs = set(config.allowedTargetRefs) - {'desktop'}
        old = {row['ref']: json.loads(row['snapshot']) for row in db.execute(
            'SELECT * FROM autonomy_target_grants WHERE role_id=?', (role_id,))}
        selected = {}
        if selections is not None:
            with self._lock:
                for item in selections:
                    observation = self._observations.get(item.selectionObservationId)
                    if (observation is None or observation['expires'] <= time.monotonic()
                            or observation['role'] != role_id or observation['revision'] != expected_revision):
                        raise TargetConflict('target selection observation expired or changed')
                    snapshot = observation['snapshot']
                    ref = target_ref(role_id, item.accountId, item.targetId)
                    if (ref not in refs or ref in selected or snapshot['accountId'] != item.accountId
                            or snapshot['targetId'] != item.targetId
                            or snapshot['directoryRevision'] != item.expectedDirectoryRevision.model_dump()):
                        raise TargetConflict('target selection does not match its original observation')
                    try:
                        current = self.host.freeze_owner_target(role_id, item.accountId, item.targetId)
                    except (PermissionError, KeyError):
                        raise TargetConflict('target owner or account permission changed') from None
                    if current != snapshot:
                        raise TargetConflict('target owner, account or adapter was replaced')
                    selected[ref] = snapshot
        if refs:
            self._shared_database()
        for ref in refs:
            snapshot = selected.get(ref) or old.get(ref)
            if snapshot is None:
                raise ValueError('external target requires an explicit observed selection')
            try:
                current = self.host.freeze_owner_target(role_id, snapshot['accountId'], snapshot['targetId'])
            except (PermissionError, KeyError):
                raise TargetConflict('saved target is no longer permitted; explicitly reselect') from None
            if _grant(current) != _grant(snapshot):
                raise TargetConflict('saved target binding changed; explicitly reselect')
            db.execute('INSERT OR REPLACE INTO autonomy_target_grants VALUES(?,?,?)',
                (role_id, ref, json.dumps(current, ensure_ascii=False)))
        for ref in old.keys() - refs:
            db.execute('DELETE FROM autonomy_target_grants WHERE role_id=? AND ref=?', (role_id, ref))

    def public(self, db, role_id, config):
        grants = {row['ref']: json.loads(row['snapshot']) for row in db.execute(
            'SELECT * FROM autonomy_target_grants WHERE role_id=?', (role_id,))}
        result = []
        facts = {item['accountId']: item for item in self.accounts(role_id)['accounts']}
        for ref in config.allowedTargetRefs:
            if ref == 'desktop':
                result.append({'ref': ref, 'channel': 'desktop', 'displayName': '当前角色桌面会话', 'current': True, 'denial': None})
                continue
            saved = grants.get(ref)
            current = False
            denial: str | None = '目标缺少原观察许可，请重新选择'
            if saved:
                try:
                    current = _grant(self.host.freeze_owner_target(role_id, saved['accountId'], saved['targetId'])) == _grant(saved)
                    denial = None if current else '原账号、目录或主人绑定已经变化'
                except (PermissionError, KeyError):
                    denial = '原目标或账号已经停用、移除或解绑'
            result.append({'ref': ref, 'channel': saved['channel'] if saved else 'unknown',
                'accountId': saved['accountId'] if saved else '', 'targetId': saved['targetId'] if saved else '',
                'displayName': saved.get('displayName', '') if saved else ref,
                'accountDisplayName': saved.get('accountDisplayName', '') if saved else '', 'current': current, 'denial': denial,
                **({key: value for key, value in facts.get(saved['accountId'], {}).items()
                    if key in {'connection', 'online', 'observedAt', 'reason'}} if saved else {})})
        return result

    def freeze(self, db, origin, config):
        targets = {}
        for ref in config.allowedTargetRefs:
            if ref == 'desktop':
                targets[ref] = {'ref': ref, 'channel': 'desktop'}
                continue
            self._shared_database()
            row = db.execute('SELECT snapshot FROM autonomy_target_grants WHERE role_id=? AND ref=?', (origin.role_id, ref)).fetchone()
            if row is None:
                raise PermissionError('external target has no original owner selection')
            saved = json.loads(row[0])
            current = self.host.freeze_owner_target(origin.role_id, saved['accountId'], saved['targetId'])
            if _grant(current) != _grant(saved):
                raise PermissionError('external target owner or configuration changed')
            targets[ref] = {'ref': ref, **current}
        db.execute('INSERT INTO autonomy_target_runs VALUES(?,?,?)', (origin.tick_id, origin.role_id, json.dumps(targets, ensure_ascii=False)))

    def current(self, db, origin):
        row = db.execute('SELECT targets FROM autonomy_target_runs WHERE tick_id=? AND role_id=?', (origin.tick_id, origin.role_id)).fetchone()
        if row is None:
            # Pre-upgrade desktop runs did not carry external target authority.
            legacy = db.execute('SELECT frozen_config FROM autonomy_runs WHERE tick_id=?', (origin.tick_id,)).fetchone()
            return bool(legacy and set(json.loads(legacy[0]).get('allowedTargetRefs', ['desktop'])) <= {'desktop'})
        try:
            for snapshot in json.loads(row[0]).values():
                if snapshot['channel'] == 'desktop':
                    continue
                self._shared_database()
                current = self.host.freeze_owner_target(origin.role_id, snapshot['accountId'], snapshot['targetId'])
                if _grant(current) != _grant({key: value for key, value in snapshot.items() if key != 'ref'}) or current['adapterGeneration'] != snapshot['adapterGeneration']:
                    return False
        except (PermissionError, KeyError, ValueError):
            return False
        return True

    def model_view(self, turn):
        """Project original run addresses; observation never grants new scope."""
        with turn.transaction() as db:
            row = db.execute('''SELECT t.targets,r.frozen_config FROM autonomy_target_runs t
                JOIN autonomy_runs r ON r.tick_id=t.tick_id AND r.role_id=t.role_id
                WHERE t.tick_id=? AND t.role_id=?''', (turn.origin.tick_id, turn.origin.role_id)).fetchone()
            if row is None:
                raise PermissionError('autonomous run has no frozen target set')
            targets, config = json.loads(row[0]), json.loads(row[1])
        refs, default = config['allowedTargetRefs'], config['defaultTargetRef']
        if not 1 <= len(refs) <= 4 or set(refs) != set(targets) or default not in targets:
            raise PermissionError('autonomous frozen target set is inconsistent')
        descriptors = []
        for ref in refs:
            item = targets[ref]
            descriptor = {'ref': ref, 'channel': item['channel']}
            if item['channel'] != 'desktop':
                descriptor.update(account_id=item['accountId'], target_id=item['targetId'])
            descriptors.append(descriptor)
        return {'defaultTargetRef': default, 'allowedTargets': descriptors}

    def selected(self, turn, arguments):
        if any(key in arguments for key in ('account_channel', 'target_kind')):
            raise ValueError('target kind and channel cannot replace an observed target pair')
        pair = ('account_id' in arguments, 'target_id' in arguments)
        if pair[0] != pair[1] or any(not arguments[key] for key, present in zip(('account_id', 'target_id'), pair) if present):
            raise ValueError('retarget requires a complete nonempty account and target pair')
        with turn.transaction() as db:
            row = db.execute('SELECT targets FROM autonomy_target_runs WHERE tick_id=?', (turn.origin.tick_id,)).fetchone()
            targets = json.loads(row[0]) if row else {'desktop': {'ref': 'desktop', 'channel': 'desktop'}}
            if pair[0]:
                selected = next((item for item in targets.values() if item.get('accountId') == arguments['account_id']
                    and item.get('targetId') == arguments['target_id']), None)
            else:
                selected = targets.get(arguments.get('targetRef', turn.config.defaultTargetRef))
            if selected is None or selected['ref'] not in turn.config.allowedTargetRefs:
                raise PermissionError('target is outside the original run grant')
            return selected
