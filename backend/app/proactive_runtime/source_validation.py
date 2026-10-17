"""Owner-trusted local artifacts prove a finite, persistent ACK contract.

Call ensure_schema in the application's existing SQLite write transaction.
The constructor does no I/O. Each instance owns one verification attempt;
the host must keep its event loop and registry alive until close succeeds.
validate_local never uses the production argv suffix, environment or vault.
This is an explicitly trusted code check, not an OS sandbox or remote test.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Callable
import uuid

from ..mcp_autonomy import declaration_hash
from ..mcp_config import McpConfigStore
from ..mcp_stdio import McpStdioClient, finish_cleanup
from .config import AutonomyConfig, SourceGrant
from .sources import _decode, _normalize
from . import retention
from .context import _check_chain, _opened_path, _path_key, _version

VERIFIER_VERSION = "meido-source-contract-v1"
AUTHOR_PROFILE = "meido-source-author-v2"
MAX_FILES = 32
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_STAGING_ENTRIES = 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_SAFE_PREFIX = frozenset({"-I", "-E", "-s", "-S", "-u", "-B", "--no-warnings", "--no-deprecation"})


def ensure_schema(db):
    if not db.in_transaction:
        raise RuntimeError("source contract schema requires an owned transaction")
    db.execute("""CREATE TABLE IF NOT EXISTS autonomy_source_contracts (
        proof_id TEXT PRIMARY KEY,role_id TEXT NOT NULL,source_id TEXT NOT NULL,
        source_binding TEXT NOT NULL,artifact TEXT NOT NULL,artifact_digest TEXT NOT NULL,
        verifier_version TEXT NOT NULL,verified_at REAL NOT NULL)""")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("contract manifest has duplicate keys")
        value[key] = item
    return value


def _binding(source):
    return _json(source.model_dump(mode="json", exclude={"ackProofId"}))


def _regular(path):
    if not path.is_absolute():
        raise ValueError("contract artifact requires an absolute path")
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise PermissionError("contract artifacts cannot use linked paths")
    if not path.is_file():
        raise ValueError("contract artifact is unavailable")
    return path.resolve()


def _read(path, limit):
    original = path.absolute()
    if _path_key(_regular(original)) != _path_key(original):
        raise ValueError("contract artifact resolved outside its selected path")
    path = original
    before = _check_chain(path)
    if before.st_nlink != 1 or before.st_size > limit:
        raise ValueError("contract artifact exceeds its byte or link budget")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened, actual = os.fstat(descriptor), _opened_path(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or _version(before) != _version(opened)
                or (actual is not None and _path_key(actual) != _path_key(path))):
            raise ValueError("contract artifact changed while opening")
        chunks, size = [], 0
        while size <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if (size > limit or _version(os.fstat(descriptor)) != _version(opened)
                or _version(_check_chain(path)) != _version(opened)):
            raise ValueError("contract artifact changed or exceeded its byte budget")
        return b''.join(chunks)
    finally:
        os.close(descriptor)


def _digest(path):
    with _regular(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _artifact(manifest_path, expected_hash, command=None):
    if not isinstance(expected_hash, str) or not _HASH.fullmatch(expected_hash):
        raise ValueError("an explicit trusted manifest SHA256 is required")
    raw = _read(manifest_path, MAX_MANIFEST_BYTES)
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise PermissionError("trusted contract manifest changed")
    try:
        manifest = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise ValueError("invalid contract manifest") from None
    if (not isinstance(manifest, dict) or set(manifest) != {"profile", "version", "entrypoint", "files"}
            or type(manifest["version"]) is not int
            or (manifest["profile"], manifest["version"]) not in {(VERIFIER_VERSION, 1), (AUTHOR_PROFILE, 2)}
            or not isinstance(manifest["entrypoint"], str)
            or not isinstance(manifest["files"], dict) or not 1 <= len(manifest["files"]) <= MAX_FILES):
        raise ValueError("unsupported contract manifest profile")
    root = manifest_path.resolve().parent
    contents, seen, total = {}, set(), 0
    for name, expected in manifest["files"].items():
        if (not isinstance(name, str) or len(name) > 240 or "\\" in name or ":" in name
                or not name or PurePosixPath(name).is_absolute()
                or any(part in {"", ".", ".."} for part in name.split("/"))
                or name.casefold() in seen or not isinstance(expected, str) or not _HASH.fullmatch(expected)):
            raise ValueError("invalid contract support file")
        seen.add(name.casefold())
        target = root.joinpath(*name.split("/"))
        if not target.resolve().is_relative_to(root):
            raise PermissionError("contract support file escaped its artifact")
        content = _read(target, MAX_ARTIFACT_BYTES - total)
        total += len(content)
        if hashlib.sha256(content).hexdigest() != expected:
            raise PermissionError("trusted contract support file changed")
        contents[name] = content
    entry = manifest["entrypoint"]
    if entry not in contents:
        raise ValueError("contract entrypoint must be a declared file")
    result = {"manifestPath": str(manifest_path.resolve()), "manifestHash": expected_hash,
              "manifest": manifest}
    if command is not None:
        actual = root.joinpath(*entry.split("/")).resolve()
        matches = [index for index, argument in enumerate(command) if index and Path(argument).is_absolute()
                   and Path(argument).resolve() == actual]
        if len(matches) != 1 or any(value not in _SAFE_PREFIX for value in command[1:matches[0]]):
            raise PermissionError("contract entrypoint is not the exact trusted interpreter script")
        executable = _regular(Path(command[0]).resolve())
        if not re.fullmatch(r"(?:python(?:\d+(?:\.\d+)*)?|node)(?:\.exe)?", executable.name, re.IGNORECASE):
            raise ValueError("contract profile supports direct Python or Node interpreters")
        result.update(prefix=list(command[:matches[0]]), interpreterPath=str(executable), interpreterHash=_digest(executable))
    return result, contents


def _current_artifact(saved):
    current, _ = _artifact(Path(saved["manifestPath"]), saved["manifestHash"])
    if (current["manifest"] != saved["manifest"]
            or str(Path(saved["prefix"][0]).resolve()) != saved["interpreterPath"]
            or _digest(Path(saved["interpreterPath"])) != saved["interpreterHash"]):
        raise PermissionError("contract executable artifact changed")
    return hashlib.sha256(_json(saved).encode()).hexdigest()


def retry_allowed(db, role_id: str, source: SourceGrant) -> bool:
    """No RPC or schema-only claim can grant a second external ACK ordinal.

The caller must additionally use current_source to check the live MCP grant.
Missing schema/proof, changed identity, artifact or verifier fail closed.
"""
    if source.ackContract != "absolute-idempotent" or source.ackProofId is None:
        return False
    try:
        row = db.execute("SELECT role_id,source_id,source_binding,artifact,artifact_digest,verifier_version FROM autonomy_source_contracts WHERE proof_id=?", (source.ackProofId,)).fetchone()
        if row is None or tuple(row[:3]) != (role_id, source.sourceId, _binding(source)) or row[5] not in {VERIFIER_VERSION, AUTHOR_PROFILE}:
            return False
        return _current_artifact(json.loads(row[3])) == row[4]
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return False


class SourceContractRegistry:
    def __init__(self, database_path: str | Path, mcp_store: McpConfigStore) -> None:
        self.database_path, self.mcp_store = str(database_path), mcp_store
        self._peers: list[McpStdioClient] = []
        self._directory: Path | None = None
        self._directory_parent: Path | None = None
        self._operation: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sealed = False

    @property
    def cleanup_pending(self) -> bool:
        return bool(self._peers or self._directory is not None or self._operation is not None and not self._operation.done())

    def _check(self, role_id, source, server, authorized, signal):
        if (self._sealed or signal is not None and signal.is_set() or not authorized()
                or self.mcp_store.get(source.serverId) != server or not server.enabled
                or role_id not in server.role_ids
                or (server.revision, server.grant_id) != (source.configRevision, source.grantRevision)):
            self._sealed = True
            raise PermissionError("source contract validation authority was revoked")
        self._check_staging()

    def _check_staging(self):
        if self._directory is None or not self._directory.exists():
            return
        pending, total, artifact_total, entries = [self._directory], 0, 0, 0
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as scan:
                for entry in scan:
                    entries += 1
                    path = Path(entry.path)
                    if entries > MAX_STAGING_ENTRIES or entry.is_symlink() or path.is_junction():
                        self._sealed = True
                        raise PermissionError("source contract staging entry budget or link policy failed")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            size = entry.stat(follow_symlinks=False).st_size
                        except FileNotFoundError:
                            # A live source may commit and remove its SQLite
                            # journal after scandir observed the entry.
                            continue
                        if path.is_relative_to(self._directory / "artifact"):
                            artifact_total += size
                        else:
                            total += size
                        if total > MAX_STATE_BYTES or artifact_total > MAX_ARTIFACT_BYTES:
                            self._sealed = True
                            raise PermissionError("source contract state/home storage budget exceeded")

    async def validate_local(self, role_id: str, source: SourceGrant, *, manifest_path: Path,
                             expected_manifest_sha256: str, authorized: Callable[[], bool],
                             signal: threading.Event | None = None, deadline: float | None = None) -> dict[str, object]:
        if self._operation is not None or self._sealed:
            raise RuntimeError("source contract registry owns exactly one attempt")
        if (source.channel != 'context' and (source.ackContract != "absolute-idempotent" or not source.ackTool)
                or source.channel == 'context' and (source.ackContract != 'none' or source.ackTool is not None)):
            raise ValueError("event verification requires absolute ACK; context has no ACK")
        server = self.mcp_store.get(source.serverId)
        if server is None:
            raise PermissionError("source MCP grant is unavailable")
        self._check(role_id, source, server, authorized, signal)
        self._loop = asyncio.get_running_loop()
        end = min(deadline if deadline is not None else time.monotonic() + 120, time.monotonic() + 120)
        operation = asyncio.create_task(self._validate(role_id, source, server, Path(manifest_path),
            expected_manifest_sha256, authorized, signal, end))
        self._operation = operation

        async def watch():
            while not operation.done():
                await asyncio.sleep(.02)
                try:
                    self._check(role_id, source, server, authorized, signal)
                except Exception:
                    operation.cancel()
                    return
        watcher = asyncio.create_task(watch())
        try:
            return await operation
        finally:
            watcher.cancel()
            await finish_cleanup(asyncio.create_task(self._join_watcher(watcher)))

    @staticmethod
    async def _join_watcher(watcher):
        await asyncio.gather(watcher, return_exceptions=True)

    async def _validate(self, role_id, source, server, manifest_path, expected_hash, authorized, signal, deadline):
        artifact = None
        try:
            async with asyncio.timeout(max(0, deadline - time.monotonic())):
                if time.monotonic() >= deadline:
                    raise TimeoutError("source contract validation deadline expired")
                artifact, files = _artifact(manifest_path, expected_hash, server.command)
                profile = artifact['manifest']['profile']
                if source.channel == 'context' and profile != AUTHOR_PROFILE:
                    raise ValueError('context collection verification requires the author profile')
                self._check(role_id, source, server, authorized, signal)
                self._directory = Path(tempfile.mkdtemp(prefix="meido-source-contract-")).resolve()
                self._directory_parent = self._directory.parent
                staging, state, home = (self._directory / name for name in ("artifact", "state", "home"))
                for directory in (staging, state, home):
                    directory.mkdir()
                for name, content in files.items():
                    target = staging.joinpath(*name.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                command = (*artifact["prefix"], str(staging / artifact["manifest"]["entrypoint"]),
                           "--meido-contract-test", str(state))
                environment = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
                environment.update(PATH=os.pathsep.join(filter(None, (str(Path(command[0]).parent),
                    str(Path(environment["SYSTEMROOT"]) / "System32") if "SYSTEMROOT" in environment else ""))),
                    TEMP=str(home), TMP=str(home), HOME=str(home), USERPROFILE=str(home),
                    APPDATA=str(home), LOCALAPPDATA=str(home), PYTHONNOUSERSITE="1", PYTHONUTF8="1")
                config = replace(server, command=command, cwd=str(staging), env_keys=(), timeout_seconds=5)
                def check():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("source contract validation deadline expired")
                    self._check(role_id, source, server, authorized, signal)
                if profile == AUTHOR_PROFILE:
                    await self._verify_author(config, environment, source, check)
                else:
                    await self._verify(config, environment, source, check)
        finally:
            await finish_cleanup(asyncio.create_task(self._dispose()))
        # Both independent processes and their stdio are joined before a proof
        # exists. A failed close retains the peers/root and prevents publishing.
        self._check(role_id, source, server, authorized, signal)
        if time.monotonic() >= deadline:
            raise TimeoutError("source contract validation deadline expired")
        digest = _current_artifact(artifact)
        identifier = uuid.uuid4().hex
        with sqlite3.connect(self.database_path, timeout=.05) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT config FROM autonomy_roles WHERE role_id=?", (role_id,)).fetchone()
            current = AutonomyConfig.model_validate_json(row[0]) if row else None
            matched = next((item for item in current.sourceGrants if item.sourceId == source.sourceId), None) if current else None
            if matched is None or _binding(matched) != _binding(source):
                raise PermissionError("source revision changed before proof publication")
            self._check(role_id, source, server, authorized, signal)
            if _current_artifact(artifact) != digest:
                raise PermissionError("source artifact changed before proof publication")
            db.execute("INSERT INTO autonomy_source_contracts VALUES(?,?,?,?,?,?,?,?)", (identifier, role_id,
                source.sourceId, _binding(source), _json(artifact), digest, profile, time.time()))
            self._check(role_id, source, server, authorized, signal)
            if time.monotonic() >= deadline:
                raise TimeoutError("source contract validation deadline expired")
            usage, limits = retention.usage(db, role_id), retention.DEFAULT_LIMITS
            if (usage["globalBytes"] > limits.global_bytes or usage["roleRecords"] > limits.role_records
                    or usage["roleBytes"] > limits.role_bytes):
                raise retention.StorageLimitError("source contract proof exceeds autonomous storage quota")
            self._check(role_id, source, server, authorized, signal)
            if time.monotonic() >= deadline:
                raise TimeoutError("source contract validation deadline expired")
        return {"proofId": identifier, "verifierVersion": profile, "artifactDigest": digest,
                "sourceId": source.sourceId, "sourceRevision": source.revision,
                "collectionVerified": profile == AUTHOR_PROFILE, "ackVerified": source.channel != 'context'}

    async def _open(self, config, environment, source, check):
        check()
        client = McpStdioClient(config, environment)
        self._peers.append(client)  # Ownership precedes the first spawn await.
        catalog = await client.connect()
        check()
        definitions = {item["name"]: declaration_hash(item) for item in catalog}
        for name, expected in ((source.getTool, source.getDefinitionHash),
                               (source.pollTool, source.pollDefinitionHash), (source.ackTool, source.ackDefinitionHash)):
            if name is not None and definitions.get(name) != expected:
                raise PermissionError("source contract tool definition changed")
        if not {"meido_test_seed", "meido_test_clock"} <= definitions.keys():
            raise ValueError("trusted source has no isolated contract-test controls")
        return client

    @staticmethod
    async def _call(client, name, arguments, check):
        check()
        result = _decode(await client.call(name, arguments))
        check()
        return result

    async def _verify_author(self, config, environment, source, check):
        """Probe only this grant's kind; a context source needs no event tools."""
        now, identifier, marker = 1700000000, uuid.uuid4().hex, uuid.uuid4().hex
        event = {'id': identifier, 'kind': source.channel, 'title': 'host collection nonce',
                 'body': marker, 'data': {'contract_nonce': marker}}
        seed = {'event_domain': source.eventDomain, 'now': now, 'events': [event], 'mode': 'ok'}
        client = await self._open(config, environment, source, check)
        context_identity: list[str] = []

        async def collection(peer):
            payload = await self._call(peer, source.getTool, {}, check)
            items, incomplete = _normalize(source, payload, lambda *_: True)
            if incomplete:
                raise ValueError('contract collection was incomplete')
            return items

        async def positive(peer):
            items = await collection(peer)
            if len(items) != 1 or source.channel != 'context' and items[0]['remoteId'] != identifier:
                raise ValueError('source event identity is not stable')
            if source.channel == 'content' and items[0]['body'] != marker:
                raise ValueError('source body is not associated with the seeded event')
            if source.channel == 'context':
                if items[0]['data'].get('contract_nonce') != marker:
                    raise ValueError('source context did not preserve the seeded data')
                if not context_identity:
                    context_identity.append(items[0]['id'])
                elif context_identity[0] != items[0]['id']:
                    raise ValueError('source context changed its fixed synthetic seed')

        await self._call(client, 'meido_test_seed', seed, check)
        await positive(client)
        await positive(client)  # An unACKed event must not disappear on read.
        await client.close()
        self._peers.remove(client)
        client = await self._open(config, environment, source, check)
        await positive(client)  # Rebuilding the server must preserve pending data.
        await self._call(client, 'meido_test_seed', {**seed, 'events': []}, check)
        if await collection(client):
            raise ValueError('source did not distinguish the empty collection')
        await self._call(client, 'meido_test_seed', {**seed, 'events': [], 'mode': 'error'}, check)
        check()
        failed = await client.call(source.getTool, {})
        check()
        if failed.get('isError') is not True:
            raise ValueError('source read failure was hidden as an empty collection')
        if source.channel != 'context':
            await self._verify(config, environment, source, check, client=client)

    async def _verify(self, config, environment, source, check, *, client=None):
        now, event = 1700000000, uuid.uuid4().hex
        finite = {"event_ids": [event], "ack_id": uuid.uuid4().hex,
                  "expires_at": now + 7200, "ttl_hours": 2}
        permanent_event = uuid.uuid4().hex
        permanent = {"event_ids": [permanent_event], "ack_id": uuid.uuid4().hex,
                     "expires_at": None, "ttl_hours": 0}
        client = client or await self._open(config, environment, source, check)
        await self._call(client, "meido_test_seed", {"event_domain": source.eventDomain, "now": now,
            "lose_first_ack_receipt": True,
            "events": [{"id": item, "kind": source.channel, "title": "host contract nonce"}
                       for item in (event, permanent_event)]}, check)

        async def events(peer):
            payload = await self._call(peer, source.getTool, {}, check)
            items, incomplete = _normalize(source, payload, lambda *_: True)
            if incomplete:
                raise ValueError("contract source did not return its complete seeded events")
            return [item["remoteId"] for item in items]

        if sorted(await events(client)) != sorted([event, permanent_event]):
            raise ValueError("contract source did not expose host-random synthetic events")
        if source.pollTool:
            await self._call(client, source.pollTool, {}, check)
        check()
        lost = await client.call(source.ackTool, finite)
        check()
        if not lost.get("isError") or await events(client) != [permanent_event]:
            raise ValueError("contract test did not prove accepted ACK with a lost receipt")
        permanent_receipt = await self._call(client, source.ackTool, permanent, check)
        if await events(client):
            raise ValueError("accepted source events were not suppressed")
        await client.close()
        self._peers.remove(client)
        client = await self._open(config, environment, source, check)
        if await events(client):
            raise ValueError("source restart lost the original event suppression")
        for request in (finite, permanent):
            first = await self._call(client, source.ackTool, request, check)
            repeated = await self._call(client, source.ackTool, request, check)
            if (not isinstance(first, dict) or first.get("event_ids") != request["event_ids"]
                    or first.get("ack_id") != request["ack_id"] or "expires_at" not in first
                    or first["expires_at"] != request["expires_at"] or first != repeated
                    or request is permanent and first != permanent_receipt):
                raise ValueError("source ACK did not preserve the original identity and receipt after restart")
            check()
            raw = await client.call(source.ackTool, {**request, "expires_at": now + 14400})
            check()
            if not raw.get("isError") and _decode(raw) != first:
                raise ValueError("source ACK changed or extended an existing acknowledgement")
        if await events(client):
            raise ValueError("source restart lost the original event suppression")
        await self._call(client, "meido_test_clock", {"now": now + 7201}, check)
        if await events(client) != [event]:
            raise ValueError("source ACK suppression did not use the fixed original expiry")
        await self._call(client, "meido_test_clock", {"now": now + 20000}, check)
        if await events(client) != [event]:
            raise ValueError("permanent source acknowledgement expired")

    async def _dispose(self):
        errors = []
        for peer in tuple(self._peers):
            try:
                await peer.close()
                if hasattr(peer, "exit_confirmed") and not peer.exit_confirmed:
                    raise RuntimeError("source contract process exit remains unconfirmed")
            except BaseException as error:
                errors.append(error)
            else:
                self._peers.remove(peer)
        if not self._peers and self._directory is not None:
            try:
                self._check_staging()
            except (OSError, ValueError) as error:
                errors.append(error)  # Still dispose the exact owned root.
            try:
                directory = self._directory
                if directory.parent != self._directory_parent or not directory.name.startswith("meido-source-contract-"):
                    raise PermissionError("source contract cleanup root changed")
                if directory.is_symlink():
                    directory.unlink()
                elif directory.is_junction():
                    directory.rmdir()
                elif directory.exists():
                    if directory.resolve() != directory:
                        raise PermissionError("source contract cleanup escaped its owned root")
                    shutil.rmtree(directory)
            except OSError as error:
                errors.append(error)
            else:
                self._directory = None
        if errors:
            raise errors[0]

    async def close(self) -> None:
        if self.cleanup_pending and self._loop is not asyncio.get_running_loop():
            raise RuntimeError("source contract cleanup requires its original live event loop")
        self._sealed = True
        operation = self._operation
        if operation is not None and not operation.done() and operation is not asyncio.current_task():
            operation.cancel()
            await finish_cleanup(asyncio.create_task(self._join_watcher(operation)))
        await finish_cleanup(asyncio.create_task(self._dispose()))
