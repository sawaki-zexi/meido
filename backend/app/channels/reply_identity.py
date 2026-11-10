"""Name a verified quote without granting its author's authority to its reader."""
from collections.abc import Mapping


def capture_reply_identity(channels, account, *, sender_id: str, own_id: str = "", verified: bool, bindings=None) -> dict:
    """Capture host identity facts only for a platform-verified quoted sender."""
    if bindings is None:
        owner = channels.identity_is_owner(role_id=account.role_id, channel=account.channel,
            scope='platform' if account.channel in {'qq', 'telegram'} else 'account',
            platform_user_id=sender_id, account_id=account.account_id)
    else:
        from .source_authority import select_owner_binding
        owner = select_owner_binding(bindings, account.channel, account.account_id, sender_id) is not None
    own_id = str((account.platform_id or own_id) if account.channel in {'qq', 'telegram'} else own_id or '')
    return {
        "replyContextVerified": verified,
        "replyToSenderId": sender_id or None,
        "receivingPlatformId": own_id or None,
        'receivingPlatformIdentityVerified': bool(own_id),
        "replyToSenderIsOwner": bool(verified and sender_id and owner),
    }


def reply_sender_identity(channels, thread, metadata: Mapping[str, object]) -> str:
    sender_id = str(metadata.get("replyToSenderId") or "")
    if not sender_id or metadata.get("replyContextVerified") is not True:
        return "未知发送者"
    # This labels a historical fact, not current authorization. Never infer it
    # from a replacement account or erase it after unpairing.
    own_id = str(metadata.get("receivingPlatformId") or "")
    if own_id and sender_id == own_id:
        return "角色本人"
    if metadata.get("replyToSenderIsOwner") is True:
        return "已配对主人"
    return "未配对成员" if metadata.get('replyToSenderIsOwner') is False else '成员（主人身份未知）'
