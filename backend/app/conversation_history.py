"""Frozen, revocable history over the original role-owned transcripts."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .context_windows import ContextWindowStore, WindowConflict, _stamp
from .session_store import load_runtime_attachments
from .channels.delivery_projection import delivered_message

_COLUMNS = "message_id,session_key,sequence,role,content,status,created_at,message_type,tool_call_id,tool_name,tool_arguments_json,tool_result_json,is_error,run_id,metadata_json"


def _instant(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _history_value(message):
    value = message.model_dump(mode="json")
    # Finishing the previous queue item can happen while the next turn loads
    # media. Invalidation is filtered by read(); other queue states carry no
    # transcript content or authority and must not cancel a valid read.
    value["metadata"].pop("channelTurnState", None)
    return value


def _grant_authority(grant):
    return {key: value for key, value in grant.items() if key != "title"}


def freeze_channel_history(channels, account):
    """Normalize no payload here: freeze the host account before adapter awaits."""
    history = ConversationHistoryView(channels, role_id=account.role_id)
    return {"account": history._account(account.account_id), "history": history.snapshot(),
        "bindings": [{key: item[key] for key in ("identityId", "channel", "scope", "platformUserId", "accountId", "pairedAt", "chats")}
            for item in channels.list_identities() if item["channel"] == account.channel
                and (item["scope"] == "platform" or item["accountId"] == account.account_id)]}


def bind_channel_history(channels, thread, sender_id, intake):
    """After normalization, select only the identity already present at intake."""
    from .channels.source_authority import channel_owner_binding
    view = ConversationHistoryView(channels, role_id=thread.role_id, snapshot=intake["history"])
    frozen = intake["account"]
    current = view._account(thread.account_id)
    if (not current or frozen["id"] != thread.account_id or frozen["createdAt"] != current["createdAt"]
            or frozen["revision"] != current["revision"]
            or frozen["platformId"] is not None and frozen["platformId"] != current["platformId"]):
        raise PermissionError("入站账号来源已变化")
    previous = next((item for item in intake["bindings"] if item["platformUserId"] == sender_id), None)
    current_binding = channel_owner_binding(channels, thread.channel, thread.account_id, sender_id)
    keys = ("identityId", "pairedAt")
    if (tuple(previous[key] for key in keys) if previous else None) != (
            tuple(current_binding[key] for key in keys) if current_binding else None):
        raise PermissionError("排队前的入站身份已变化")
    if previous is None or thread.chat_type != "private":
        return None
    view.require_current()
    channels.remember_owner_chat(role_id=thread.role_id, channel=thread.channel, scope=previous["scope"],
        platform_user_id=sender_id, account_id=thread.account_id, chat_id=thread.chat_id)
    snapshot = view.snapshot()
    with channels._connect() as db:
        source = view._grants(db).get(thread.session_key)
    if source is None or (source["identityId"], source["pairedAt"]) != tuple(previous[key] for key in keys):
        raise PermissionError("入站私聊尚未获得历史授权")
    if thread.session_key not in snapshot["grants"]:
        known = any(chat["roleId"] == thread.role_id and chat["accountId"] == thread.account_id
            and chat["chatId"] == thread.chat_id and chat["contextSince"] == source["since"] for chat in previous["chats"])
        if not known and _instant(source["since"]) < view.admitted_at:
            raise PermissionError("不能扩大排队前的历史来源")
        snapshot["grants"][thread.session_key] = source
    snapshot.update(sessionKey=thread.session_key, sourceAccount=current)
    return snapshot


class ConversationHistoryView:
    """Capture grants before waiting; a later binding can never enlarge a turn."""
    def __init__(self, channels, *, role_id, thread_id=None, owner=True, through_message_id=None,
                 attachment_resolver=None, authorized=lambda: True, snapshot=None):
        self.channels, self.sessions = channels, channels.sessions
        self.role_id, self.thread_id = role_id, thread_id or f"desktop:{role_id}"
        self.session_key = f"role:{role_id}" if self.thread_id == f"desktop:{role_id}" else f"thread:{self.thread_id}"
        self.attachment_resolver, self.host_authorized = attachment_resolver, authorized
        self.current_id = through_message_id
        self.admitted_at = datetime.now(timezone.utc)
        self._sealed_ids = None
        self._sealed_fingerprint = None
        if snapshot is not None:
            if snapshot.get("roleId") != role_id or snapshot.get("sessionKey") != self.session_key:
                raise PermissionError("历史来源与当前角色不匹配")
            self.grants, self.fence, self.shared = snapshot["grants"], snapshot["fence"], snapshot["shared"]
            self.current_id = snapshot.get("currentId")
            self.admitted_at = _instant(snapshot["admittedAt"])
        else:
            with channels._connect() as db:
                db.execute("BEGIN")
                self.grants = self._grants(db) if owner else {}
                row = db.execute("SELECT rowid FROM messages WHERE message_id=? AND session_key=? AND role='user'",
                    (through_message_id, self.session_key)).fetchone() if through_message_id else None
                if through_message_id and row is None:
                    raise PermissionError("当前消息不属于此历史来源")
                self.fence = int(row[0]) if row else int(db.execute("SELECT COALESCE(MAX(rowid),0) FROM messages").fetchone()[0])
            self.shared = owner and (self.session_key == f"role:{role_id}" or self.session_key in self.grants)
            if not self.shared:
                self.grants = {}
        thread = channels.get_thread(role_id, self.thread_id) if not self.thread_id.startswith("desktop:") else None
        self.source_account = snapshot.get("sourceAccount") if snapshot is not None else self._account(thread.account_id) if thread else None
        if self.session_key.startswith("thread:") and (thread is None or self.source_account is None):
            raise PermissionError("当前角色无权读取此会话历史")

    def _account(self, account_id):
        account = self.channels.get_account(account_id)
        return {"id": account_id, "createdAt": account.created_at, "platformId": account.platform_id,
            "revision": list(self.channels.directory_revision(account_id, account.channel))} if account and account.role_id == self.role_id else None

    def _grants(self, db):
        rows = db.execute("""SELECT t.session_key,t.thread_id,t.account_id,t.title,t.channel,t.chat_id,
            c.context_since,i.identity_id,i.paired_at FROM channel_threads t
            JOIN channel_accounts a ON a.account_id=t.account_id AND a.role_id=t.role_id AND a.channel=t.channel
            JOIN channel_owner_chats c ON c.role_id=t.role_id AND c.channel=t.channel AND c.account_id=t.account_id AND c.chat_id=t.chat_id
            JOIN channel_identities i ON i.channel=c.channel AND i.scope=c.scope AND i.platform_user_id=c.platform_user_id
              AND (i.scope='platform' OR i.account_id=c.account_id) AND i.owner=1
            WHERE t.role_id=? AND t.chat_type='private' AND COALESCE(json_extract(t.metadata_json,'$.accountRemoved'),0)=0""", (self.role_id,)).fetchall()
        return {str(row[0]): {"threadId": row[1], "account": self._account(row[2]), "title": row[3], "channel": row[4],
            "chatId": row[5], "since": row[6], "identityId": row[7], "pairedAt": row[8]} for row in rows}

    def snapshot(self):
        return json.loads(json.dumps({"roleId": self.role_id, "sessionKey": self.session_key,
            "grants": self.grants, "fence": self.fence, "shared": self.shared,
            "sourceAccount": self.source_account, "currentId": self.current_id,
            "admittedAt": self.admitted_at.isoformat()}))

    @property
    def authority(self):
        return _stamp({"role": self.role_id, "shared": self.shared,
            "grants": {key: _grant_authority(grant) for key, grant in self.grants.items()}, "source": self.source_account})

    def include_current(self, message_id):
        if self._sealed_ids is not None:
            raise RuntimeError("历史围栏已固定")
        self.current_id = message_id

    def require_current(self):
        if not self.host_authorized():
            raise PermissionError("历史来源已失效")
        if self.source_account is not None and self._account(self.source_account["id"]) != self.source_account:
            raise PermissionError("历史来源账号已变化")
        with self.channels._connect() as db:
            current = self._grants(db)
        if any(_grant_authority(current.get(session, {})) != _grant_authority(grant) for session, grant in self.grants.items()):
            raise PermissionError("主人历史绑定已变化，请重新开始当前回合")

    def authorized(self):
        try:
            self.require_snapshot_current()
            return True
        except (PermissionError, KeyError):
            return False

    def require_snapshot_current(self):
        self.require_current()
        if self._sealed_ids is not None:
            self.read()

    def read(self):
        self.require_current()
        sessions = {f"role:{self.role_id}", *self.grants} if self.shared else {self.session_key}
        with self.channels._connect() as db:
            db.execute("BEGIN")
            rows = []
            for session in sessions:
                rows.extend(db.execute("SELECT rowid," + _COLUMNS + " FROM messages WHERE session_key=? ORDER BY sequence", (session,)).fetchall())
        entries = [(int(row[0]), self.sessions._message(tuple(row)[1:])) for row in rows]
        sources = {(message.sessionKey, message.runId): (order, message) for order, message in entries
            if message.role == "user" and message.runId and message.messageType == "text"}
        selected = []
        for order, message in entries:
            message = delivered_message(message)
            if message is None:
                continue
            source = sources.get((message.sessionKey, message.runId))
            anchor = source[0] if source else order
            anchor_date = source[1].createdAt if source else message.createdAt
            if self._sealed_ids is not None and message.id not in self._sealed_ids:
                continue
            if ((anchor > self.fence or anchor_date > self.admitted_at) and message.id != self.current_id) or message.status != "completed":
                continue
            if message.metadata.get("channelTurnState") == "invalidated":
                continue
            if message.metadata.get("internalEvent") and not (
                    message.id == self.current_id and message.metadata['internalEvent'] == 'herdr_agent_report'
                    or message.role == "assistant" and message.messageType in {"internal_completion", "desktop_push"}
                    and message.metadata.get("deliveryStatus") == "sent"):
                continue
            grant = self.grants.get(message.sessionKey) if self.shared else None
            if grant:
                since = _instant(grant["since"])
                if message.createdAt < since or (source and source[1].createdAt < since):
                    continue
                if (message.role == "user" and message.metadata.get("senderIsOwner") is False
                        or source and source[1].metadata.get("senderIsOwner") is False):
                    continue
                if source and source[1].metadata.get("channelTurnState") == "invalidated":
                    continue
                # A late reply with no trustworthy turn link cannot prove it
                # answers a post-pairing input. Keep it only in its original UI.
                if message.runId and message.role != "user" and source is None:
                    continue
            if (message.sessionKey.startswith("thread:") and message.role == "assistant"
                    and message.messageType == "text" and message.metadata.get("deliveryStatus") != "sent"
                    and not message.metadata.get('deliveryProjection')):
                continue
            selected.append((anchor, order, message))
        selected.sort(key=lambda item: (item[0], item[1]))
        self.require_current()
        fingerprint = _stamp([_history_value(message) for _, _, message in selected])
        if self._sealed_fingerprint is not None and fingerprint != self._sealed_fingerprint:
            raise PermissionError("历史消息在当前回合中已变化")
        if self._sealed_ids is None:
            self._sealed_ids = {message.id for _, _, message in selected}
            self._sealed_fingerprint = fingerprint
        return [message for _, _, message in selected]

    def runtime_messages(self, *, max_messages=None):
        originals = self.read()
        visible = {message.id: message for message in originals}
        platform_messages = {(message.sessionKey, str(message.metadata.get("platformMessageId"))): message
            for message in originals if message.metadata.get("platformMessageId")}
        prepared = []
        for message in originals:
            grant = self.grants.get(message.sessionKey)
            label = f"{grant['channel']} / {grant['title'] or grant['chatId']}" if grant else "桌面" if message.sessionKey.startswith("role:") else self.thread_id
            metadata = dict(message.metadata)
            content = message.content
            if message.role == "user":
                attachments = list(metadata.get("attachments") or [])
                if metadata.get("replyTo"):
                    quoted = platform_messages.get((message.sessionKey, str(metadata["replyTo"]))) or visible.get(str(metadata["replyTo"]))
                    if quoted is not None and quoted.sessionKey == message.sessionKey:
                        content = f"引用{'主人' if quoted.role == 'user' else '角色'}：\n{quoted.content}\n\n{content}"
                        attachments.extend(quoted.metadata.get("attachments") or [])
                    else:
                        content = "[引用原文不在当前授权历史中或已缺失]\n" + content
                metadata["attachments"] = attachments
                metadata.pop("quotedAttachments", None)
                metadata.pop("replyToContent", None)
                content = f"[历史来源：{label}；message_id={message.id}]\n" + content
            elif message.role == "assistant" and message.messageType in {"text", "internal_completion", "desktop_push"}:
                content = f"[回复来源：{label}]\n" + content
                blocks = metadata.get("content_blocks")
                if isinstance(blocks, list):
                    metadata["content_blocks"] = [{"type": "text", "text": f"[回复来源：{label}]"},
                        *(block for block in blocks if isinstance(block, dict) and block.get("type") != "image")]
                media = self._attachments(metadata)
                if media:
                    blocks = metadata.setdefault("content_blocks", [{"type": "text", "text": content}])
                    for item in media:
                        if item.image:
                            blocks.append({"type": "image", "media_type": item.image.media_type,
                                "data_base64": item.image.data_base64, "detail": item.image.detail})
                        elif item.text:
                            blocks.append({"type": "text", "text": f"[{item.name}]\n{item.text}"})
            prepared.append(message.model_copy(update={"content": content, "metadata": metadata,
                **({'messageType': 'text'} if message.id == self.current_id
                    and message.messageType == 'herdr_agent_report' else {})}))
        result = self.sessions.runtime_messages(self.session_key, stored_messages=prepared,
            attachment_loader=self._attachments, max_messages=max_messages)
        self.require_current()
        # A removal while media was being read must invalidate the returned data.
        if [_history_value(message) for message in self.read()] != [_history_value(message) for message in originals]:
            raise PermissionError("历史消息在读取期间变化")
        return result

    def _attachments(self, metadata):
        self.require_current()
        loaded = []
        for item in metadata.get("attachments", []):
            if not isinstance(item, dict):
                continue
            self.require_current()
            resolved = self.attachment_resolver(str(item.get("id") or "")) if self.attachment_resolver else None
            self.require_current()
            if resolved and Path(str(resolved.get("path", ""))).is_file():
                values = load_runtime_attachments({"attachments": [resolved]})
            else:
                values = ()
            if values:
                loaded.extend(values)
            else:
                from .agent_runtime.types import AttachmentContent
                loaded.append(AttachmentContent(str(item.get("name") or "附件"), "text/plain", text="[历史附件已缺失或不属于当前角色]"))
        self.require_current()
        return tuple(loaded)

    def windows(self):
        return HistoryWindowStore(self)


class HistoryWindowStore(ContextWindowStore):
    """Existing compaction ledger, with fingerprints over the same visible IDs."""
    def __init__(self, view):
        self.view = view
        super().__init__(view.sessions.database_path)

    def fingerprint(self, db, session, ids):
        self.view.require_current()
        visible = {message.id: message for message in self.view.read()}
        if any(identifier not in visible for identifier in ids):
            raise WindowConflict("工作摘要包含当前不可见的来源")
        return _stamp([_history_value(visible[identifier]) for identifier in ids])
