from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Protocol


class CredentialStore(Protocol):
    """Small seam for storing secrets outside application databases."""

    def get(self, key: str) -> str: ...

    def set(self, key: str, value: str) -> None: ...


class SystemCredentialStore:
    """Keep Feishu secrets in the operating system credential vault."""

    _SERVICE = "meido.owner-knowledge"
    _CHUNK_MARKER = "meido.credential-chunks.v1:"
    # Windows Credential Manager accepts at most 1280 UTF-16 code units in a
    # generic credential blob. Leave headroom for backend encoding details.
    _CHUNK_SIZE_UNITS = 1000

    @staticmethod
    def _utf16_units(value: str) -> int:
        return len(value.encode("utf-16-le")) // 2

    @classmethod
    def _split_value(cls, value: str) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        current_units = 0
        for character in value:
            character_units = cls._utf16_units(character)
            if current and current_units + character_units > cls._CHUNK_SIZE_UNITS:
                chunks.append("".join(current))
                current = []
                current_units = 0
            current.append(character)
            current_units += character_units
        if current:
            chunks.append("".join(current))
        return chunks

    @classmethod
    def _chunk_metadata(cls, value: str) -> tuple[str, int, int] | None:
        if not value.startswith(cls._CHUNK_MARKER):
            return None
        parts = value.removeprefix(cls._CHUNK_MARKER).split(":")
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{32}", parts[0]):
            return None
        try:
            count, total_units = int(parts[1]), int(parts[2])
        except ValueError:
            return None
        if count < 1 or total_units < 1:
            return None
        return parts[0], count, total_units

    @staticmethod
    def _chunk_key(key: str, generation: str, index: int) -> str:
        return f"{key}.chunk.{generation}.{index}"

    @classmethod
    def _delete_chunks(cls, keyring: Any, key: str, metadata: tuple[str, int, int]) -> None:
        generation, count, _total_units = metadata
        for index in range(count):
            try:
                keyring.delete_password(cls._SERVICE, cls._chunk_key(key, generation, index))
            except keyring.errors.PasswordDeleteError:
                pass

    @classmethod
    def _delete_chunks_best_effort(cls, keyring: Any, key: str, metadata: tuple[str, int, int]) -> None:
        try:
            cls._delete_chunks(keyring, key, metadata)
        except Exception as error:
            logging.getLogger(__name__).warning(
                "清理旧的系统凭据分片失败：%s", type(error).__name__
            )

    def get(self, key: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]

            stored_value = keyring.get_password(self._SERVICE, key) or ""
            metadata = self._chunk_metadata(stored_value)
            if metadata is None:
                return stored_value
            generation, count, total_units = metadata
            chunks = [
                keyring.get_password(self._SERVICE, self._chunk_key(key, generation, index))
                for index in range(count)
            ]
            if any(chunk is None for chunk in chunks):
                return ""
            value = "".join(chunk for chunk in chunks if chunk is not None)
            return value if self._utf16_units(value) == total_units else ""
        except Exception:
            # An unavailable vault means the optional Feishu connection is not
            # configured. Writes still fail loudly below so secrets are never
            # silently persisted somewhere else.
            return ""

    def set(self, key: str, value: str) -> None:
        try:
            import keyring  # type: ignore[import-not-found]

            old_value = keyring.get_password(self._SERVICE, key) or ""
            old_metadata = self._chunk_metadata(old_value)
            if not value:
                try:
                    keyring.delete_password(self._SERVICE, key)
                except keyring.errors.PasswordDeleteError:
                    pass
                if old_metadata is not None:
                    self._delete_chunks_best_effort(keyring, key, old_metadata)
                return

            chunks = self._split_value(value)
            if len(chunks) == 1:
                keyring.set_password(self._SERVICE, key, value)
                if old_metadata is not None:
                    self._delete_chunks_best_effort(keyring, key, old_metadata)
                return

            generation = uuid.uuid4().hex
            total_units = self._utf16_units(value)
            staged_keys: list[str] = []
            try:
                for index, chunk in enumerate(chunks):
                    chunk_key = self._chunk_key(key, generation, index)
                    keyring.set_password(self._SERVICE, chunk_key, chunk)
                    staged_keys.append(chunk_key)
                marker = f"{self._CHUNK_MARKER}{generation}:{len(chunks)}:{total_units}"
                keyring.set_password(self._SERVICE, key, marker)
            except Exception:
                for chunk_key in staged_keys:
                    try:
                        keyring.delete_password(self._SERVICE, chunk_key)
                    except Exception:
                        pass
                raise

            if old_metadata is not None:
                self._delete_chunks_best_effort(keyring, key, old_metadata)
        except Exception as error:
            raise RuntimeError("系统凭据库不可用，未保存飞书凭据") from error
