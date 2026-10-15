from __future__ import annotations

import base64
from typing import Protocol


class SecretStore(Protocol):
    def get(self, key: str) -> str: ...
    def set(self, key: str, value: str) -> None: ...


def credential_key(channel: str, account_id: str, name: str) -> str:
    """Build a collision-free key for the shared system credential vault."""

    parts = (channel, account_id, name)
    if any(not isinstance(part, str) or not part for part in parts):
        raise ValueError("渠道凭据键包含非法字符")
    encoded = [base64.urlsafe_b64encode(part.encode("utf-8")).decode("ascii").rstrip("=") for part in parts]
    return "channel." + ".".join(encoded)


class NamespacedCredentials:
    def __init__(self, store: SecretStore, *, channel: str, account_id: str) -> None:
        self.store = store
        self.channel = channel
        self.account_id = account_id

    def get(self, name: str) -> str:
        return self.store.get(credential_key(self.channel, self.account_id, name))

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("凭据不能为空")
        self.store.set(credential_key(self.channel, self.account_id, name), value)
