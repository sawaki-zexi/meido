"""Safe, format-neutral character card preview import."""

from __future__ import annotations

import base64
import binascii
import io
import json
import mimetypes
import posixpath
import re
import struct
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any


MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_CHARX_MEMBERS = 128
MAX_CHARX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_CHARX_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ASSET_PREVIEW_BYTES = 10 * 1024 * 1024
MAX_ASSET_PREVIEW_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ASSET_PREVIEW_COUNT = 24
SUPPORTED_EXTENSIONS = {".json", ".png", ".apng", ".charx"}
MACRO_PATTERN = re.compile(r"\{\{[^{}]+\}\}")
IMAGE_EXTENSIONS = {"png", "apng", "jpg", "jpeg", "webp", "gif", "unknown"}


class RoleCardImportError(ValueError):
    """A user-facing import validation or format error."""


@dataclass(frozen=True)
class ImportAsset:
    kind: str
    name: str
    media_type: str
    size: int
    preview_base64: str | None = None
    archive_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"kind": self.kind, "name": self.name or self.archive_path or "未命名素材", "mediaType": self.media_type, "size": self.size}
        if self.preview_base64 is not None:
            value["dataUrl"] = f"data:{self.media_type};base64,{self.preview_base64}"
        return value


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        text = _clean_text(data.get(key))
        if text:
            return text
    return ""


def _as_card_object(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise RoleCardImportError("角色卡内容必须是 JSON 对象")
    nested = value.get("data")
    if nested is not None and not isinstance(nested, dict):
        raise RoleCardImportError("角色卡 data 必须是 JSON 对象")
    return (nested if isinstance(nested, dict) else value), value


def _macros(value: Any) -> list[str]:
    if isinstance(value, str):
        return MACRO_PATTERN.findall(value)
    if isinstance(value, dict):
        return [item for child in value.values() for item in _macros(child)]
    if isinstance(value, list):
        return [item for child in value for item in _macros(child)]
    return []


def _declared_assets(card: dict[str, Any]) -> tuple[list[ImportAsset], list[str]]:
    declarations: list[tuple[str, str, str, str]] = []
    raw_assets = card.get("assets")
    if isinstance(raw_assets, list):
        for index, item in enumerate(raw_assets):
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("type", "name", "uri", "ext")):
                continue
            kind, name, uri, extension = (item[key].strip() for key in ("type", "name", "uri", "ext"))
            if kind == "user_icon" or (uri != "ccdefault:" and extension not in IMAGE_EXTENSIONS):
                continue
            if uri == "ccdefault:" and kind == "icon":
                declarations.append(("avatar", name, uri, "image/png"))
            elif uri.startswith("embeded://"):
                declarations.append(("avatar" if kind == "icon" and name == "main" else "background" if kind == "background" and name == "main" else "emotion" if kind == "emotion" else "asset", name, uri.removeprefix("embeded://"), ""))
            else:
                # Remote/data URIs are reported as unsupported; they are never fetched.
                declarations.append(("unsupported", name, uri, ""))
    for key, kind in (("avatar", "avatar"), ("icon", "avatar"), ("background", "background"), ("background_image", "background")):
        value = card.get(key)
        if isinstance(value, str) and value.strip():
            declarations.append((kind, "", value.strip(), ""))
    for key in ("emotion_images", "emotions"):
        value = card.get(key)
        if isinstance(value, dict):
            declarations.extend(
                ("emotion", str(name).strip(), path.strip(), "")
                for name, path in value.items()
                if isinstance(path, str) and path.strip()
            )
        elif isinstance(value, list):
            declarations.extend(
                ("emotion", str(item.get("name", "")).strip(), item["path"].strip(), "")
                for item in value
                if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip()
            )
    unsupported: list[str] = []
    assets: list[ImportAsset] = []
    for kind, name, uri, media_type in declarations:
        if uri == "ccdefault:":
            continue
        if kind == "unsupported" or (":" in uri and not uri.startswith("embeded://")):
            unsupported.append(uri)
            continue
        try:
            path = _safe_archive_path(uri.removeprefix("embeded://"))
        except RoleCardImportError:
            unsupported.append(uri)
            continue
        assets.append(ImportAsset(kind, name, media_type or (mimetypes.guess_type(path)[0] or "application/octet-stream"), 0, archive_path=path))
    return assets, unsupported


def _draft_from_card(value: Any, *, source: str, assets: list[ImportAsset] | None = None, asset_warnings: list[str] | None = None) -> dict[str, Any]:
    card, envelope = _as_card_object(value)
    name = _first_text(card, "name", "char_name", "character_name") or "未命名角色"
    description = _first_text(card, "description")
    extensions = card.get("extensions")
    shiori = extensions.get("shiori") if isinstance(extensions, dict) else None
    summary = _clean_text(shiori.get("description")) if isinstance(shiori, dict) else ""
    context = _first_text(card, "char_context", "context")
    scenario = _first_text(card, "world_scenario")
    profile = "\n\n".join(part for part in (description, context, scenario) if part)
    behavior_rules = _first_text(card, "system_prompt", "systemPrompt", "system")
    response_constraints = _first_text(card, "post_history_instructions", "postHistoryInstructions", "response_constraints", "responseConstraints")
    personality = _first_text(card, "personality", "persona", "char_persona")
    nickname = _first_text(card, "nickname", "user_name", "userName")
    known = {"name", "char_name", "character_name", "description", "char_context", "context", "world_scenario", "personality", "persona", "char_persona", "system_prompt", "systemPrompt", "system", "post_history_instructions", "postHistoryInstructions", "response_constraints", "responseConstraints", "nickname", "user_name", "userName", "extensions", "assets", "avatar", "icon", "background", "background_image", "emotion_images", "emotions"}
    discarded = [key for key, item in card.items() if key not in known and item not in (None, "", [], {})]
    warnings: list[str] = []
    for key, label in (("char_greeting", "开场白"), ("first_mes", "开场白"), ("first_message", "开场白"), ("alternate_greetings", "备用开场白"), ("creator_notes", "作者备注"), ("tags", "标签"), ("character_book", "世界书"), ("scenario", "场景"), ("mes_example", "示例对话"), ("example_dialogue", "示例对话")):
        if card.get(key):
            discarded.append(key)
            warnings.append(f"{label}未映射到角色字段")
    unsupported_macros = [macro for macro in dict.fromkeys(_macros(envelope)) if macro.lower() not in {"{{char}}", "{{user}}"}]
    if unsupported_macros:
        warnings.append("包含未处理的模板宏")
    if asset_warnings:
        warnings.append(f"有 {len(asset_warnings)} 个素材无法导入")
    manual_assets = [asset for asset in (assets or []) if asset.kind != "avatar"]
    if manual_assets:
        warnings.append(f"有 {len(manual_assets)} 个非头像素材，需要手动选择导入目标")
    unavailable_assets = [asset for asset in (assets or []) if asset.size == 0]
    if unavailable_assets:
        warnings.append(f"有 {len(unavailable_assets)} 个素材只有路径，角色卡未包含可导入数据")
    if discarded:
        warnings.append(f"有 {len(set(discarded))} 个字段未直接映射")
    return {"draft": {"name": name, "description": summary, "profile": profile, "personality": personality, "behaviorRules": behavior_rules, "responseConstraints": response_constraints, "nickname": nickname}, "source": source, "assets": [asset.as_dict() for asset in (assets or [])], "compatibility": {"format": source.rsplit(".", 1)[-1].lower() if "." in source else "json", "warnings": list(dict.fromkeys(warnings)), "discardedFields": list(dict.fromkeys(discarded)), "unsupportedMacros": unsupported_macros, "unsupportedResources": list(dict.fromkeys(asset_warnings or []))}}


def _decode_json(raw: bytes) -> Any:
    if len(raw) > MAX_JSON_BYTES:
        raise RoleCardImportError("角色卡 JSON 不能超过 10 MB")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RoleCardImportError("角色卡不是有效的 UTF-8 JSON") from error


def _decode_metadata(text: bytes) -> Any:
    if len(text) > MAX_METADATA_BYTES:
        raise RoleCardImportError("PNG 角色卡 metadata 过大")
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        decoded = text
    try:
        return _decode_json(decoded)
    except RoleCardImportError:
        import zlib
        try:
            return _decode_json(zlib.decompress(decoded))
        except (zlib.error, RoleCardImportError) as error:
            raise RoleCardImportError("PNG 角色卡 metadata 不是有效 JSON") from error


def _png_metadata(raw: bytes) -> tuple[Any, list[ImportAsset]]:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RoleCardImportError("文件不是有效的 PNG/APNG")
    position = 8
    metadata: dict[str, bytes] = {}
    while position + 12 <= len(raw):
        length = struct.unpack(">I", raw[position:position + 4])[0]
        kind = raw[position + 4:position + 8]
        end = position + 12 + length
        if end > len(raw):
            raise RoleCardImportError("PNG chunk 超出文件范围")
        chunk = raw[position + 8:position + 8 + length]
        if kind == b"tEXt":
            key, separator, value = chunk.partition(b"\x00")
            if separator and key in (b"chara", b"ccv3"):
                metadata[key.decode("ascii")] = value
        elif kind == b"iTXt":
            keyword, separator, remainder = chunk.partition(b"\x00")
            if separator and keyword in (b"chara", b"ccv3") and len(remainder) >= 2:
                compressed = remainder[0] == 1
                remainder = remainder[2:]
                _, separator, remainder = remainder.partition(b"\x00")
                if separator:
                    _, separator, text = remainder.partition(b"\x00")
                    if separator:
                        if compressed:
                            import zlib
                            try:
                                text = zlib.decompress(text)
                            except zlib.error as error:
                                raise RoleCardImportError("PNG 角色卡 metadata 解压失败") from error
                        metadata[keyword.decode("ascii")] = text
        elif kind == b"zTXt":
            key, separator, compressed = chunk.partition(b"\x00")
            if separator and key in (b"chara", b"ccv3"):
                import zlib
                try:
                    metadata[key.decode("ascii")] = zlib.decompress(compressed[1:])
                except zlib.error as error:
                    raise RoleCardImportError("PNG 角色卡 metadata 解压失败") from error
        position = end
        if kind == b"IEND":
            break
    encoded = metadata.get("ccv3") or metadata.get("chara")
    if encoded is None:
        raise RoleCardImportError("PNG 中没有找到 chara 或 ccv3 metadata")
    card = _decode_metadata(encoded)
    media_type = "image/png"
    asset = ImportAsset("avatar", "main", media_type, len(raw), base64.b64encode(raw).decode("ascii") if len(raw) <= MAX_ASSET_PREVIEW_BYTES else None)
    return card, [asset]


def _safe_archive_path(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = normalized.rstrip("/").split("/")
    if not normalized or path.is_absolute() or ":" in parts[0] or any(part in ("", ".", "..") for part in parts):
        raise RoleCardImportError("CHARX 包含不安全的文件路径")
    return path.as_posix()


def _charx(raw: bytes) -> tuple[Any, list[ImportAsset], list[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError) as error:
        raise RoleCardImportError("文件不是有效的 CHARX 压缩包") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_CHARX_MEMBERS:
            raise RoleCardImportError("CHARX 文件数量超过限制")
        total = 0
        card_info = None
        assets: list[ImportAsset] = []
        unsupported: list[str] = []
        seen: set[str] = set()
        normalized_infos: list[tuple[zipfile.ZipInfo, str]] = []
        for info in infos:
            name = _safe_archive_path(info.filename)
            if name in seen:
                raise RoleCardImportError("CHARX 包含重复路径")
            seen.add(name)
            if info.flag_bits & 0x1:
                raise RoleCardImportError("不支持加密的 CHARX 文件")
            if ((info.external_attr >> 16) & 0xF000) == 0xA000:
                raise RoleCardImportError("CHARX 不允许包含符号链接")
            if info.is_dir():
                continue
            if info.file_size > MAX_CHARX_MEMBER_BYTES:
                raise RoleCardImportError("CHARX 单个文件超过 25 MB")
            total += info.file_size
            if total > MAX_CHARX_UNCOMPRESSED_BYTES:
                raise RoleCardImportError("CHARX 解压总大小超过限制")
            if name == "card.json":
                card_info = info
            normalized_infos.append((info, name))
        if card_info is None:
            raise RoleCardImportError("CHARX 根目录缺少 card.json")
        card_json = archive.read(card_info)
        card = _decode_json(card_json)
        declared, declared_unsupported = _declared_assets(_as_card_object(card)[0])
        unsupported.extend(declared_unsupported)
        declared_paths = {asset.archive_path or asset.name for asset in declared}
        preview_bytes = 0
        preview_count = 0

        def preview_for(data: bytes) -> str | None:
            nonlocal preview_bytes, preview_count
            if len(data) > MAX_ASSET_PREVIEW_BYTES or preview_count >= MAX_ASSET_PREVIEW_COUNT:
                return None
            if preview_bytes + len(data) > MAX_ASSET_PREVIEW_TOTAL_BYTES:
                return None
            preview_bytes += len(data)
            preview_count += 1
            return base64.b64encode(data).decode("ascii")

        for info, name in normalized_infos:
            if info.is_dir() or name == "card.json" or name in declared_paths:
                continue
            media_type = mimetypes.guess_type(info.filename)[0] or ""
            if media_type.startswith("image/"):
                data = archive.read(info)
                preview = preview_for(data)
                assets.append(ImportAsset("asset", info.filename, media_type, len(data), preview))
        for asset in declared:
            try:
                info = archive.getinfo(asset.archive_path or asset.name)
            except KeyError:
                unsupported.append(asset.archive_path or asset.name)
                continue
            data = archive.read(info)
            media_type = mimetypes.guess_type(info.filename)[0] or asset.media_type
            preview = preview_for(data)
            assets.append(ImportAsset(asset.kind, asset.name, media_type, len(data), preview))
        return card, assets, unsupported


def parse_role_card(filename: str, raw: bytes) -> dict[str, Any]:
    filename = filename.replace(chr(92), "/").rsplit("/", 1)[-1]
    result = _parse_role_card(filename, raw)
    result["importProvenance"] = {
        "format": posixpath.splitext(filename.lower())[1][1:],
        "source": filename,
        "importedAt": datetime.now(timezone.utc).isoformat(),
    }
    return result


def _parse_role_card(filename: str, raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_SOURCE_BYTES:
        raise RoleCardImportError("角色卡文件不能超过 50 MB")
    suffix = posixpath.splitext(filename.lower())[1]
    if suffix not in SUPPORTED_EXTENSIONS:
        raise RoleCardImportError("仅支持 .json、.png、.apng 或 .charx 角色卡")
    if suffix == ".json":
        card, _ = _as_card_object(_decode_json(raw))
        assets, unsupported = _declared_assets(card)
        return _draft_from_card(_decode_json(raw), source=filename, assets=assets, asset_warnings=unsupported)
    if suffix in (".png", ".apng"):
        card, assets = _png_metadata(raw)
        return _draft_from_card(card, source=filename, assets=assets)
    card, assets, unsupported = _charx(raw)
    return _draft_from_card(card, source=filename, assets=assets, asset_warnings=unsupported)
