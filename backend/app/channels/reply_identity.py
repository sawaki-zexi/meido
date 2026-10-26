"""Name a verified quote without granting its author's authority to its reader."""
from collections.abc import Mapping


def capture_reply_identity(channels, account, *, sender_id: str, own_id: str = "", verified: bool) -> dict:
    """Capture host identity facts only for a platform-verified quoted sender."""
    return {
        "replyContextVerified": verified,
        "replyToSenderId": sender_id or None,
        "receivingPlatformId": str(account.platform_id or own_id),
        "replyToSenderIsOwner": bool(verified and sender_id and channels.identity_is_owner(
            role_id=account.role_id, channel=account.channel,
            scope="platform" if account.channel in {"qq", "telegram"} else "account",
            platform_user_id=sender_id, account_id=account.account_id,
        )),
    }


def reply_sender_identity(channels, thread, metadata: Mapping[str, object]) -> str:
    sender_id = str(metadata.get("replyToSenderId") or "")
    if not sender_id or metadata.get("replyContextVerified") is not True:
        return "未知发送者"
    account = channels.get_account(thread.account_id)
    if account is None or account.role_id != thread.role_id or account.channel != thread.channel:
        return "未知发送者"
    own_id = str(account.platform_id or metadata.get("receivingPlatformId") or "")
    if own_id and sender_id == own_id:
        return "角色本人"
    if metadata.get("replyToSenderIsOwner") is True and channels.identity_is_owner(
        role_id=thread.role_id, channel=thread.channel,
        scope="platform" if thread.channel in {"qq", "telegram"} else "account",
        platform_user_id=sender_id, account_id=thread.account_id,
    ):
        return "已配对主人"
    return "未配对成员"
