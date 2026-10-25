# Adapted from Shiori-Agent; Copyright (c) 2026 YinFengWindy (MIT).
# See docs/reference/third-party-notices.md.
"""Cache verified QQ dependencies without coupling to desktop QQ updates."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
from pathlib import Path
from uuid import uuid4

QQ_VERSION = "9.9.31-49738"
_EXPECTED_SHA256 = {
    "crypto.dll": "2a143d944cd80ba373242deacd06ad47523c07191a4a322e4336e62062d2f5f3",
    "ssl.dll": "fbadd7a295e8d932295c5b59c086b1a9074f61f347694674b4c76ecff5c5965c",
    "wrapper.node": "a1e59891e743c271d641ee011f47aa887d9f7dfae6b3bb9292a03af759dec203",
}
# QQ 9.9.36-53644, validated Tencent signatures and native QR startup on Windows.
_UPDATED_DEPENDENCIES = {
    "crypto.dll": "cedfbad8dbdb7d4356c9b0869e703f515caafd59b3192cd8864a8bac244ff662",
    "ssl.dll": "fca95c1ea07952b4f894012fc186b0ded35679f0cdf7707d7c64fa9274961553",
}
_UNINSTALL_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_root() -> Path:
    if platform.system() != "Windows":
        raise RuntimeError("托管 NapCat 仅支持 Windows x64")
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_name in _UNINSTALL_KEYS:
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
            except (FileNotFoundError, OSError):
                continue
            if not isinstance(uninstall, str):
                continue
            executable = Path(uninstall.strip().strip('"'))
            if (
                not executable.is_absolute()
                or executable.name.lower() != "uninstall.exe"
                or not executable.is_file()
                or not (executable.parent / "QQ.exe").is_file()
            ):
                continue
            return executable.parent
    raise RuntimeError(f"未找到官方 QQ {QQ_VERSION} 安装目录（Windows 卸载注册表）")


def resolve_official_qq(napcat_dir: Path) -> Path:
    """Use the pinned wrapper and privately cache only compatible native DLLs."""
    bundled_wrapper = napcat_dir / "wrapper.node"
    if (
        not bundled_wrapper.is_file()
        or _sha256(bundled_wrapper) != _EXPECTED_SHA256["wrapper.node"]
    ):
        raise RuntimeError("NapCat 官方组件 wrapper.node 缺失或摘要不匹配")

    cache = napcat_dir / "qq-runtime"
    if not cache.resolve().is_relative_to(napcat_dir.resolve()):
        raise ValueError("托管 QQ 目录无效")
    if _verified_dependencies(cache):
        return cache

    root = _install_root()
    version_config = root / "versions" / "config.json"
    if not version_config.is_file():
        raise RuntimeError("QQ 安装目录缺少 versions/config.json")
    try:
        version = json.loads(version_config.read_text(encoding="utf-8")).get(
            "curVersion"
        )
    except (OSError, ValueError, AttributeError) as exc:
        raise RuntimeError("QQ versions/config.json 无效") from exc
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+-\d+", version):
        raise RuntimeError("QQ versions/config.json 无效")

    app_dir = root / "versions" / version / "resources" / "app"
    if not _verified_dependencies(app_dir):
        raise RuntimeError("QQ 运行组件缺失或不兼容，请安装官方 QQ 9.9.31 或 9.9.36 后重试，或手动连接 NapCat")
    staging = napcat_dir / f".qq-runtime-{uuid4().hex}"
    try:
        staging.mkdir()
        for name in ("crypto.dll", "ssl.dll"):
            shutil.copyfile(app_dir / name, staging / name)
        if not _verified_dependencies(staging):
            raise RuntimeError("QQ 运行组件在准备期间发生变化，请重试")
        if cache.exists():
            shutil.rmtree(cache)
        staging.replace(cache)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return cache


def _verified_dependencies(directory: Path) -> bool:
    try:
        actual = {name: _sha256(directory / name) for name in ("crypto.dll", "ssl.dll")}
    except OSError:
        return False
    return any(
        all(actual[name] == expected[name] for name in actual)
        for expected in (_EXPECTED_SHA256, _UPDATED_DEPENDENCIES)
    )
