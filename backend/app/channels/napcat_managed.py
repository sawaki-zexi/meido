"""Role-owned local QQ login instances; no platform secrets enter public status.

Adapted from Shiori-Agent, Copyright (c) 2026 YinFengWindy (MIT).
See docs/reference/third-party-notices.md.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import uuid
from pathlib import Path
from typing import Any

import httpx

from ..browser_use.process import spawn_process_tree, release_process_tree_owner
from ..browser_use.profile import ProfileLease
from .napcat_managed_install import NapCatInstaller

_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def managed_available() -> bool:
    return platform.system() == "Windows" and platform.machine().lower() in {"amd64", "x86_64"}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name("." + uuid.uuid4().hex[:16] + ".tmp")
    try:
        staging.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)


def _free_port(excluded: set[int]) -> int:
    for attempt in range(64):
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", 0 if attempt == 0 else 49152 + secrets.randbelow(16384)))
            except OSError:
                continue
            port = int(listener.getsockname()[1])
            if port not in excluded:
                return port
    raise RuntimeError("没有可用的托管 QQ 本机端口")


class ManagedNapCat:
    """Keep account refs durable while connections and login state remain live."""

    def __init__(self, data_dir: Path, *, process_factory=spawn_process_tree,
                 http_client: httpx.AsyncClient | None = None, poll_interval: float = .5) -> None:
        base = Path(data_dir).resolve()
        self.root = base / "managed-napcat"
        if not self.root.resolve().is_relative_to(base):
            raise ValueError("托管 QQ 目录无效")
        self._installer = NapCatInstaller(base)
        self._spawn = process_factory
        self._client = http_client
        self._own_client = http_client is None
        self._poll_interval = poll_interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._logins: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._api_locks: dict[str, asyncio.Lock] = {}
        self._credentials: dict[str, str] = {}
        self._ipc_secrets: dict[str, dict[str, str]] = {}
        self._qr_digests: dict[str, tuple[str, str]] = {}

    def _directory(self, ref: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", ref):
            raise ValueError("托管 QQ 登录引用无效")
        directory = self.root / "accounts" / ref
        if not directory.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("托管 QQ 目录无效")
        return directory

    def _path(self, ref: str, relative: str) -> Path:
        directory = self._directory(ref)
        path = directory / relative
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("托管 QQ 目录无效")
        return path

    def _metadata(self, ref: str, role_id: str) -> dict[str, Any]:
        path = self._path(ref, "instance.json")
        if not path.is_file():
            raise KeyError("托管 QQ 登录不存在")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if metadata.get("role_id") != role_id:
            raise PermissionError("托管 QQ 登录属于其他角色")
        return metadata

    def _runtime_metadata(self, ref: str, role_id: str) -> dict[str, Any]:
        metadata = self._metadata(ref, role_id)
        if ref not in self._ipc_secrets:
            self._ipc_secrets[ref] = {"onebot_token": secrets.token_hex(32), "webui_token": secrets.token_hex(32)}
        return {**metadata, **self._ipc_secrets[ref]}

    def begin(self, role_id: str) -> str:
        if not role_id.strip():
            raise ValueError("角色不能为空")
        ref = uuid.uuid4().hex
        reserved: set[int] = set()
        for path in (self.root / "accounts").glob("*/instance.json"):
            saved = json.loads(path.read_text(encoding="utf-8"))
            reserved.update((saved["onebot_port"], saved["webui_port"]))
        first = _free_port(reserved)
        second = _free_port(reserved | {first})
        _write_json(self._path(ref, "instance.json"), {
            "role_id": role_id, "expected_uin": "", "onebot_port": first,
            "webui_port": second,
        })
        return ref

    def endpoint(self, ref: str, *, role_id: str) -> tuple[str, str]:
        """Internal transport credential, never include this in an HTTP response."""
        metadata = self._runtime_metadata(ref, role_id)
        return f"ws://127.0.0.1:{metadata['onebot_port']}", metadata["onebot_token"]

    async def status(self, ref: str, *, role_id: str) -> dict[str, Any]:
        """Read cached state without waiting for an installer or network call."""
        self._metadata(ref, role_id)
        return {"ref": ref, "available": managed_available(),
                "preparation": self._public_preparation(),
                "login": copy.deepcopy(self._logins.get(ref, self._login("stopped")))}

    @staticmethod
    def _login(phase: str, *, error: str = "", **extra: Any) -> dict[str, Any]:
        return {"phase": phase, "qrcode": "", "error": error, **extra}

    def _public_preparation(self) -> dict[str, Any]:
        state = self._installer.preparation()
        if state.get("error"):
            state["error"] = _public_error(RuntimeError(str(state["error"])))
        return state

    async def connect(self, ref: str, *, role_id: str, expected_uin: str = "") -> None:
        async with self._locks.setdefault(ref, asyncio.Lock()):
            metadata = self._runtime_metadata(ref, role_id)
            if not managed_available():
                raise RuntimeError("托管 NapCat 仅支持 Windows x64")
            saved = str(metadata.get("expected_uin") or "")
            if expected_uin and (not expected_uin.isdecimal() or (saved and expected_uin != saved)):
                raise ValueError("QQ 登录身份与已绑定账号不匹配")
            running = self._tasks.get(ref)
            if running is not None and not running.done():
                return
            self._logins[ref] = self._login("starting")
            self._tasks[ref] = asyncio.create_task(self._run(ref, metadata, saved or expected_uin), name=f"managed-qq-{ref}")

    def remember_identity(self, ref: str, *, role_id: str, uin: str) -> None:
        metadata = self._metadata(ref, role_id)
        login = self._logins.get(ref, {})
        saved = str(metadata.get("expected_uin") or "")
        if not uin.isdecimal() or login.get("phase") != "online" or login.get("uin") != uin or (saved and saved != uin):
            raise ValueError("QQ 登录身份与已绑定账号不匹配")
        metadata["expected_uin"] = uin
        _write_json(self._path(ref, "instance.json"), metadata)

    async def _run(self, ref: str, metadata: dict[str, Any], expected_uin: str) -> None:
        process = owner = lease = None
        try:
            try:
                lease = ProfileLease(self._directory(ref))
            except RuntimeError:
                raise RuntimeError("托管 QQ 登录已被另一运行实例占用") from None
            runtime = await self._installer.prepare()
            self._write_configs(ref, metadata, expected_uin)
            self._clear_qr(ref)
            environment = self._environment(ref, metadata, runtime, expected_uin)
            with self._path(ref, "process.log").open("ab") as output:
                process, owner = await self._spawn(
                    str(self._installer.install_dir / "node.exe"),
                    str(self._installer.install_dir / "index.js"),
                    cwd=str(self._directory(ref)), env=environment, stdout=output,
                    stderr=asyncio.subprocess.STDOUT,
                )
            while process.returncode is None:
                try:
                    async with self._api_locks.setdefault(ref, asyncio.Lock()):
                        login = await self._read_login(ref, metadata)
                    pinned = self._metadata(ref, metadata["role_id"]).get("expected_uin") or expected_uin
                    if login["phase"] == "online" and pinned and login.get("uin") != pinned:
                        raise ValueError("QQ 登录身份与已绑定账号不匹配")
                    self._logins[ref] = login
                except (httpx.HTTPError, KeyError, RuntimeError):
                    self._logins[ref] = self._login("starting", error="QQ 登录服务暂未响应，请稍后重试")
                await asyncio.sleep(self._poll_interval)
            raise RuntimeError("托管 QQ 进程已退出，请重新连接")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._logins[ref] = self._login("error", error=_public_error(error))
        finally:
            try:
                if owner is not None:
                    await release_process_tree_owner(owner)
            finally:
                try:
                    if process is not None:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                finally:
                    if lease is not None:
                        try:
                            self._clear_runtime(ref)
                        finally:
                            lease.close()
                    self._credentials.pop(ref, None)
                    self._ipc_secrets.pop(ref, None)

    def _write_configs(self, ref: str, metadata: dict[str, Any], expected_uin: str) -> None:
        network = {"network": {"websocketServers": [{
            "name": "meido", "enable": True, "host": "127.0.0.1",
            "port": metadata["onebot_port"], "token": metadata["onebot_token"],
            "messagePostFormat": "array", "reportSelfMessage": False,
        }]}}
        _write_json(self._path(ref, "napcat/config/onebot11.json"), network)
        if expected_uin:
            _write_json(self._path(ref, f"napcat/config/onebot11_{expected_uin}.json"), network)
        _write_json(self._path(ref, "napcat/config/webui.json"), {
            "host": "127.0.0.1", "port": metadata["webui_port"], "token": metadata["webui_token"],
        })

    def _environment(self, ref: str, metadata: dict[str, Any], runtime: Path, expected_uin: str) -> dict[str, str]:
        profile = self._path(ref, "profile")
        for relative in ("", "AppData/Roaming", "AppData/Local", "Temp", "Public"):
            self._path(ref, "profile/" + relative).mkdir(parents=True, exist_ok=True)
        environment = {key: value for key, value in os.environ.items() if key not in {
            "NAPCAT_QUICK_ACCOUNT", "NAPCAT_QUICK_PASSWORD", "NAPCAT_QUICK_PASSWORD_MD5",
        }}
        environment.update({
            "USERPROFILE": str(profile), "HOME": str(profile),
            "APPDATA": str(profile / "AppData/Roaming"), "LOCALAPPDATA": str(profile / "AppData/Local"),
            "TEMP": str(profile / "Temp"), "TMP": str(profile / "Temp"), "PUBLIC": str(profile / "Public"),
            "PATH": os.pathsep.join((str(self._installer.install_dir), str(runtime), os.environ.get("PATH", ""))),
            "NAPCAT_WORKDIR": str(self._path(ref, "napcat")),
            "NAPCAT_WEBUI_PREFERRED_PORT": str(metadata["webui_port"]),
            "NAPCAT_WEBUI_SECRET_KEY": metadata["webui_token"],
            "NAPCAT_WEBUI_JWT_SECRET_KEY": metadata["webui_token"],
        })
        if expected_uin:
            environment["NAPCAT_QUICK_ACCOUNT"] = expected_uin
        return environment

    async def _request(self, ref: str, metadata: dict[str, Any], route: str) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=3, trust_env=False)
        base = f"http://127.0.0.1:{metadata['webui_port']}/api"
        credential = self._credentials.get(ref)
        if credential is None:
            response = await self._client.post(base + "/auth/login", json={
                "hash": hashlib.sha256((metadata["webui_token"] + ".napcat").encode()).hexdigest(),
            })
            response.raise_for_status()
            result = response.json()
            if result.get("code") != 0:
                raise RuntimeError("QQ 本机登录服务鉴权失败，请重新连接")
            credential = str(result["data"]["Credential"])
            self._credentials[ref] = credential
        response = await self._client.post(base + "/QQLogin/" + route, json={}, headers={"Authorization": f"Bearer {credential}"})
        if response.status_code in {401, 403}:
            self._credentials.pop(ref, None)
        response.raise_for_status()
        result = response.json()
        if result.get("code") != 0:
            self._credentials.pop(ref, None)
            raise RuntimeError("QQ 本机登录服务请求失败，请重试")
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("QQ 本机登录服务返回无效状态")
        return data

    async def _read_login(self, ref: str, metadata: dict[str, Any]) -> dict[str, Any]:
        data = await self._request(ref, metadata, "CheckLoginStatus")
        if data.get("isLogin") is True:
            identity = await self._request(ref, metadata, "GetQQLoginInfo")
            uin = str(identity.get("uin") or "")
            if not uin.isdecimal() or identity.get("online") is False:
                return self._login("starting")
            self._clear_qr(ref)
            return self._login("online", uin=uin, nickname=str(identity.get("nick") or ""))
        return self._login("login_required", qrcode=self._qr_image(ref, str(data.get("qrcodeurl") or "")),
                           error="QQ 登录未完成，请刷新二维码后重试" if data.get("loginError") else "",
                           loginPhase=str(data.get("loginPhase") or ""))

    def _clear_qr(self, ref: str) -> None:
        self._qr_digests.pop(ref, None)
        self._path(ref, "napcat/cache/qrcode.png").unlink(missing_ok=True)

    def _clear_runtime(self, ref: str) -> None:
        self._clear_qr(ref)
        self._path(ref, "napcat/config/webui.json").unlink(missing_ok=True)
        config = self._path(ref, "napcat/config")
        for path in config.glob("onebot11*.json"):
            self._path(ref, str(path.relative_to(self._directory(ref)))).unlink(missing_ok=True)

    def _qr_image(self, ref: str, url: str, *, reject_digest: str = "") -> str:
        path = self._path(ref, "napcat/cache/qrcode.png")
        if not url or not path.is_file():
            return ""
        with path.open("rb") as source:
            image = source.read(2 * 1024 * 1024 + 1)
        if len(image) > 2 * 1024 * 1024 or not image.startswith(b"\x89PNG\r\n\x1a\n") or not image.endswith(_PNG_END):
            return ""
        digest = hashlib.sha256(image).hexdigest()
        previous_url, previous_digest = self._qr_digests.get(ref, ("", ""))
        if digest == reject_digest or (previous_url != url and previous_digest == digest):
            return ""
        self._qr_digests[ref] = (url, digest)
        import base64
        return "data:image/png;base64," + base64.b64encode(image).decode("ascii")

    async def refresh_qrcode(self, ref: str, *, role_id: str) -> dict[str, Any]:
        self._metadata(ref, role_id)
        try:
            return await self._refresh_qrcode(ref, role_id=role_id)
        except (httpx.HTTPError, OSError, KeyError, ValueError) as error:
            raise RuntimeError(_public_error(error)) from None

    async def _refresh_qrcode(self, ref: str, *, role_id: str) -> dict[str, Any]:
        metadata = self._runtime_metadata(ref, role_id)
        running = self._tasks.get(ref)
        if running is None or running.done():
            raise RuntimeError("QQ 尚未连接，请先连接账号")
        async with self._api_locks.setdefault(ref, asyncio.Lock()):
            previous_url, previous_digest = self._qr_digests.get(ref, ("", ""))
            result = await self._request(ref, metadata, "RefreshQRcode")
            self._logins[ref] = self._login("starting" if result.get("restarting") else "login_required")
            url = str(result.get("qrcodeurl") or "")
            if not result.get("restarting"):
                if not url or url == previous_url:
                    raise RuntimeError("QQ 未返回新的登录二维码，请稍后重试")
                for _ in range(30):
                    image = self._qr_image(ref, url, reject_digest=previous_digest)
                    if image:
                        self._logins[ref]["qrcode"] = image
                        break
                    await asyncio.sleep(.1)
                else:
                    raise RuntimeError("QQ 新二维码尚未准备好，请稍后重试")
        return await self.status(ref, role_id=role_id)

    async def stop(self, ref: str, *, role_id: str) -> None:
        async with self._locks.setdefault(ref, asyncio.Lock()):
            # Account/vault deletion may be retried after this profile was
            # already removed. Never suppress a surviving profile's ownership
            # check, or a missing metadata file while its process is still live.
            if not self._directory(ref).exists() and ref not in self._tasks:
                self._logins.pop(ref, None)
                self._ipc_secrets.pop(ref, None)
                return
            self._metadata(ref, role_id)
            task = self._tasks.pop(ref, None)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            lease = ProfileLease(self._directory(ref))
            try:
                self._clear_runtime(ref)
            finally:
                lease.close()
            self._logins[ref] = self._login("stopped")
            self._ipc_secrets.pop(ref, None)

    async def delete(self, ref: str, *, role_id: str) -> None:
        await self.stop(ref, role_id=role_id)
        async with self._locks.setdefault(ref, asyncio.Lock()):
            if not self._directory(ref).exists():
                return
            self._metadata(ref, role_id)
            lease = ProfileLease(self._directory(ref))
            lease.close()
            await asyncio.to_thread(shutil.rmtree, self._directory(ref))
            self._logins.pop(ref, None)

    async def logout(self, ref: str, *, role_id: str) -> None:
        await self.stop(ref, role_id=role_id)
        async with self._locks.setdefault(ref, asyncio.Lock()):
            self._metadata(ref, role_id)
            lease = ProfileLease(self._directory(ref))
            try:
                profile = self._path(ref, "profile")
                if profile.exists():
                    await asyncio.to_thread(shutil.rmtree, profile)
            finally:
                lease.close()

    def _owners(self) -> tuple[tuple[str, str], ...]:
        directory = self.root / "accounts"
        if not directory.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("托管 QQ 目录无效")
        if not directory.exists():
            return ()
        owners = []
        for path in sorted(directory.iterdir()):
            if not re.fullmatch(r"[0-9a-f]{32}", path.name) or not path.is_dir():
                continue
            metadata_path = self._path(path.name, "instance.json")
            if not metadata_path.is_file():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                owner = metadata["role_id"]
                if not isinstance(owner, str) or not owner:
                    raise ValueError("missing owner")
            except (OSError, ValueError, KeyError, TypeError):
                raise RuntimeError("托管 QQ 本机账号资料损坏，无法清理") from None
            owners.append((path.name, owner))
        return tuple(owners)

    def refs_for_role(self, role_id: str) -> tuple[str, ...]:
        """List owned refs without exposing private storage details to the host."""
        return tuple(ref for ref, owner in self._owners() if owner == role_id)

    async def delete_role(self, role_id: str) -> None:
        """Call only after role deletion commits; failure can be retried on startup."""
        for ref in self.refs_for_role(role_id):
            await self.delete(ref, role_id=role_id)

    async def cleanup_deleted_roles(self, existing_role_ids: set[str]) -> None:
        for ref, owner in self._owners():
            if owner not in existing_role_ids:
                await self.delete(ref, role_id=owner)

    async def close(self) -> None:
        for ref in list(self._tasks):
            path = self._path(ref, "instance.json")
            metadata = json.loads(path.read_text(encoding="utf-8"))
            await self.stop(ref, role_id=metadata["role_id"])
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def _public_error(error: Exception) -> str:
    message = str(error)
    safe = ("托管 NapCat 仅支持 Windows x64", "NapCat 下载文件 SHA-256 校验失败",
            "NapCat 压缩包包含非法路径", "托管 QQ 登录已被另一运行实例占用",
            "QQ 登录身份与已绑定账号不匹配", "托管 QQ 进程已退出，请重新连接")
    if message in safe:
        return message
    if "未找到官方 QQ" in message or "托管 NapCat 需要 QQ" in message:
        return "托管 QQ 需要已安装官方 QQ 9.9.31-49738，请检查 QQ 版本后重试"
    return "托管 QQ 准备或连接失败，请检查网络、本机 QQ 安装和端口后重试"
