"""Freeze a real inbound source; later checks can only withdraw its authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore
    from ..session_store import SessionStore


def channel_owner_binding(
    channels: MultichannelStore, channel: str, account_id: str, sender_id: str
) -> dict[str, object] | None:
    scope = "platform" if channel in {"telegram", "qq"} else "account"
    return next(
        (
            item
            for item in channels.list_identities()
            if item["channel"] == channel
            and item["scope"] == scope
            and item["platformUserId"] == sender_id
            and (scope == "platform" or item["accountId"] == account_id)
        ),
        None,
    )


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("channel authority timestamps require a timezone")
    return result


@dataclass(frozen=True, slots=True)
class FrozenChannelSource:
    role_id: str
    session_key: str
    thread_id: str
    account_id: str
    account_created_at: str
    channel: str
    chat_id: str
    chat_type: str
    sender_id: str
    owner_context: bool
    identity_id: str
    paired_at: str
    message_created_at: str
    owner_context_since: str | None

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if key == "owner_context":
                if type(value) is not bool:
                    raise ValueError("owner authority must be a host boolean")
            elif key == "owner_context_since":
                if value is not None and not isinstance(value, str):
                    raise ValueError("invalid owner context boundary")
            elif not isinstance(value, str):
                raise ValueError("channel source fields must be strings")
        if (
            not all(
                (
                    self.role_id,
                    self.thread_id,
                    self.account_id,
                    self.channel,
                    self.chat_id,
                    self.sender_id,
                )
            )
            or self.session_key != f"thread:{self.thread_id}"
            or self.chat_type not in {"private", "group"}
        ):
            raise ValueError("invalid channel source scope")
        created = _timestamp(self.message_created_at)
        if _timestamp(self.account_created_at) > created:
            raise ValueError("message predates its channel account generation")
        if self.owner_context:
            if (
                not self.identity_id
                or not self.paired_at
                or _timestamp(self.paired_at) > created
            ):
                raise ValueError("message predates its owner binding")
            if self.chat_type == "private" and (
                self.owner_context_since is None
                or _timestamp(self.owner_context_since) > created
            ):
                raise ValueError("message predates its private owner context")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> FrozenChannelSource:
        return cls(**cast(dict[str, Any], dict(payload)))

    def authorization_denial(self, channels: MultichannelStore) -> str | None:
        account = channels.get_account(self.account_id)
        if (
            account is None
            or (account.role_id, account.channel, account.created_at)
            != (self.role_id, self.channel, self.account_created_at)
            or not channels.account_effectively_enabled(account)
        ):
            return "source channel account permission was revoked"
        if (
            self.sender_id in account.blocked_senders
            or self.chat_type == "private"
            and not account.allow_private
            or self.chat_type == "group"
            and not account.allow_group
        ):
            return "source channel conversation permission was revoked"
        thread = channels.get_thread(self.role_id, self.thread_id)
        if thread is None or (
            thread.session_key,
            thread.account_id,
            thread.channel,
            thread.chat_id,
            thread.chat_type,
        ) != (
            self.session_key,
            self.account_id,
            self.channel,
            self.chat_id,
            self.chat_type,
        ):
            return "source channel thread no longer matches the frozen origin"
        if self.owner_context:
            binding = channel_owner_binding(channels, self.channel, self.account_id, self.sender_id)
            if binding is None or (binding["identityId"], binding["pairedAt"]) != (
                self.identity_id,
                self.paired_at,
            ):
                return "source owner permission was revoked or rebound"
            if (
                self.chat_type == "private"
                and channels.owner_chat_context_since(
                    role_id=self.role_id,
                    channel=self.channel,
                    account_id=self.account_id,
                    chat_id=self.chat_id,
                )
                != self.owner_context_since
            ):
                return "source private owner context was revoked or rebound"
        return None


def capture_channel_source(
    channels: MultichannelStore,
    sessions: SessionStore,
    *,
    role_id: str,
    session_key: str,
    run_id: str,
    owner_context: bool,
) -> FrozenChannelSource:
    thread = channels.get_thread(role_id, session_key.removeprefix("thread:"))
    run = sessions.get_run(run_id)
    message = sessions.user_message_for_run(run_id)
    if (
        not session_key.startswith("thread:")
        or thread is None
        or thread.session_key != session_key
        or run is None
        or (run.roleId, run.sessionKey) != (role_id, session_key)
        or message is None
        or message.sessionKey != session_key
    ):
        raise PermissionError(
            "channel source requires the actual run inbound message and thread"
        )
    return capture_channel_message_source(channels, thread, message, owner_context=owner_context)


def capture_channel_message_source(channels, thread, message, *, owner_context: bool) -> FrozenChannelSource:
    """Freeze an already-persisted input before it waits for the shared role lock."""
    role_id, session_key = thread.role_id, thread.session_key
    if message.sessionKey != session_key or message.role != "user":
        raise PermissionError("channel source requires its actual inbound message")
    metadata = message.metadata
    if (
        metadata.get("channel"),
        metadata.get("accountId"),
        metadata.get("threadId"),
        metadata.get("direction"),
    ) != (thread.channel, thread.account_id, thread.thread_id, "inbound"):
        raise PermissionError("run inbound message does not match its channel source")
    sender_id = metadata.get("senderId")
    if not isinstance(sender_id, str) or not sender_id:
        raise PermissionError("channel source has no actual sender")
    binding = (
        channel_owner_binding(channels, thread.channel, thread.account_id, sender_id)
        if owner_context
        else None
    )
    if owner_context and (metadata.get("senderIsOwner") is not True or binding is None):
        raise PermissionError("original channel sender was not an authorized owner")
    account = channels.get_account(thread.account_id)
    if account is None:
        raise PermissionError("source channel account permission was revoked")
    try:
        source = FrozenChannelSource(
            role_id,
            session_key,
            thread.thread_id,
            thread.account_id,
            account.created_at,
            thread.channel,
            thread.chat_id,
            thread.chat_type,
            sender_id,
            owner_context,
            str(binding["identityId"]) if binding else "",
            str(binding["pairedAt"]) if binding else "",
            message.createdAt.isoformat(),
            channels.owner_chat_context_since(
                role_id=role_id,
                channel=thread.channel,
                account_id=thread.account_id,
                chat_id=thread.chat_id,
            )
            if owner_context and thread.chat_type == "private"
            else None,
        )
    except ValueError as error:
        raise PermissionError(str(error)) from error
    denial = source.authorization_denial(channels)
    if denial is not None:
        raise PermissionError(denial)
    return source
