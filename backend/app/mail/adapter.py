from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Iterable

from .models import MailConnection


class MailAdapterError(RuntimeError):
    """A safe, user-facing mailbox operation error."""


@dataclass(frozen=True, slots=True)
class RemoteMail:
    uid: str
    folder: str
    message_id: str
    thread_id: str
    sender: str
    reply_to: str
    recipients: str
    subject: str
    received_at: str
    body_text: str
    in_reply_to: str = ""
    references: str = ""


@dataclass(frozen=True, slots=True)
class RemoteDraft:
    marker: str
    uid: str
    folder: str
    message_id: str
    subject: str
    recipients: str
    body: str
    raw_hash: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (TypeError, ValueError):
        return value


def _thread_id(message_id: str, in_reply_to: str, references: str, mailbox_account: str = "") -> str:
    roots = [value.strip() for value in (references or "").split() if value.strip()]
    root = roots[0] if roots else (in_reply_to.strip() or message_id.strip())
    value = f"{mailbox_account.strip().casefold()}\0{root}" if mailbox_account else root
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _draft_hash(subject: str, recipients: str, body: str, in_reply_to: str = "", references: str = "") -> str:
    value = "\0".join((subject.strip(), recipients.strip(), body.replace("\r\n", "\n").strip(), in_reply_to.strip(), references.strip()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _body(message: email.message.Message) -> str:
    if message.is_multipart():
        parts = list(message.walk())
        plain = [item for item in parts if item.get_content_type() == "text/plain" and not item.get_filename()]
        html = [item for item in parts if item.get_content_type() == "text/html" and not item.get_filename()]
        candidates = plain or html
        value = "\n".join(str(item.get_content()) for item in candidates[:3]).strip()
        if candidates and candidates[0].get_content_type() == "text/html":
            value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
            value = re.sub(r"(?s)<[^>]+>", " ", value)
            value = email.utils.unquote(value)
        return re.sub(r"[ \t]+", " ", value).strip()
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = message.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace").strip()
    return str(message.get_payload() or "").strip()


def _folder_name(raw: bytes | str) -> tuple[set[str], str] | None:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    match = re.match(r"^\((?P<flags>[^)]*)\)\s+(?P<delim>\S+)\s+(?P<name>.*)$", text)
    if not match:
        return None
    name = match.group("name").strip().strip('"')
    flags = {flag.casefold() for flag in match.group("flags").split()}
    return flags, name


class QQMailAdapter:
    """IMAP-only QQ mailbox adapter. It never issues SMTP or mutating mail commands."""

    def __init__(self, connection: MailConnection, password: str, *, timeout: float = 25.0) -> None:
        self.connection = connection
        self.password = password
        self.timeout = timeout
        self._imap: imaplib.IMAP4_SSL | None = None

    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.connection.use_ssl:
            raise MailAdapterError("QQ 邮箱仅允许使用 SSL IMAP 连接")
        try:
            client = imaplib.IMAP4_SSL(self.connection.host, self.connection.port, timeout=self.timeout)
            client.login(self.connection.username, self.password)
        except imaplib.IMAP4.error as error:
            detail = str(error).casefold()
            if any(token in detail for token in ("auth", "login", "password", "credential", "fail")):
                raise MailAdapterError("QQ 邮箱认证失败，请确认已开启 IMAP 服务并使用邮箱授权码") from error
            raise MailAdapterError("QQ 邮箱拒绝了 IMAP 请求，请检查邮箱服务设置") from error
        except socket.gaierror as error:
            raise MailAdapterError("无法解析 QQ 邮箱 IMAP 地址，请检查网络 DNS") from error
        except (TimeoutError, socket.timeout) as error:
            raise MailAdapterError("连接 QQ 邮箱超时，请检查网络后重试") from error
        except OSError as error:
            raise MailAdapterError("无法连接 QQ 邮箱，请检查网络和 993 端口访问") from error
        self._imap = client
        return client

    def close(self) -> None:
        if self._imap is None:
            return
        try:
            self._imap.logout()
        except (OSError, imaplib.IMAP4.error):
            pass
        finally:
            self._imap = None

    def folders(self) -> dict[str, str]:
        client = self._imap or self._connect()
        try:
            status, rows = client.list()
            if status != "OK":
                raise MailAdapterError("QQ 邮箱文件夹列表不可访问")
        except (OSError, imaplib.IMAP4.error) as error:
            if isinstance(error, (TimeoutError, socket.timeout)):
                raise MailAdapterError("读取 QQ 邮箱文件夹超时，请检查网络") from error
            raise MailAdapterError("QQ 邮箱文件夹列表不可访问，请确认 IMAP 权限") from error
        found: dict[str, str] = {}
        for row in rows or []:
            parsed = _folder_name(row)
            if parsed is None:
                continue
            flags, name = parsed
            try:
                name = imaplib.IMAP4._decode_utf7(name.encode("ascii"))
            except (UnicodeEncodeError, AttributeError, ValueError):
                pass
            if "\\inbox" in flags or name.casefold() == "inbox":
                found["inbox"] = name
            elif "\\sent" in flags:
                found["sent"] = name
            elif "\\drafts" in flags:
                found["drafts"] = name
            elif name.casefold() in {"sent", "sent mail", "sent items", "sent messages", "已发送", "已发送邮件"}:
                found["sent"] = name
            elif name.casefold() in {"draft", "drafts", "drafts folder", "草稿", "草稿箱"}:
                found["drafts"] = name
        if set(found) != {"inbox", "sent", "drafts"}:
            missing = {"inbox", "sent", "drafts"}.difference(found)
            raise MailAdapterError(f"QQ 邮箱缺少可访问文件夹：{', '.join(sorted(missing))}")
        return found

    def verify(self, *, write_test: bool = False) -> dict[str, object]:
        client = self._imap or self._connect()
        folders = self.folders()
        counts: dict[str, int] = {}
        try:
            for key, folder in folders.items():
                status, data = client.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    raise MailAdapterError(f"QQ 邮箱文件夹不可访问：{key}")
                counts[key] = int((data or [b"0"])[0] or b"0")
        except (OSError, imaplib.IMAP4.error, ValueError) as error:
            if isinstance(error, MailAdapterError):
                raise
            raise MailAdapterError("QQ 邮箱文件夹不可访问") from error
        result: dict[str, object] = {"folders": folders, "counts": counts, "testDraft": None}
        if write_test:
            marker = f"meido-test-{uuid.uuid4().hex}"
            subject = f"[Meido 连接测试] {marker}"
            message = EmailMessage()
            message["From"] = self.connection.username
            message["To"] = self.connection.username
            message["Subject"] = subject
            message["X-Meido-Test-ID"] = marker
            message.set_content("这是 Meido 的 QQ 邮箱连接测试草稿。请在 QQ 邮箱网页端确认它可编辑，并可由主人手动发送；Meido 不会发送它。")
            status, response = client.append(f'"{folders["drafts"]}"', '(\\Draft)', None, message.as_bytes())
            if status != "OK":
                raise MailAdapterError("QQ 草稿箱不可写入")
            result["testDraft"] = {"marker": marker, "subject": subject, "appendResponse": str(response)}
        return result

    def _fetch(self, folder: str, query: str = "ALL", limit: int | None = None) -> list[tuple[str, bytes]]:
        client = self._imap or self._connect()
        status, _ = client.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise MailAdapterError(f"QQ 邮箱文件夹不可访问：{folder}")
        status, data = client.uid("SEARCH", None, query)
        if status != "OK":
            raise MailAdapterError(f"QQ 邮箱搜索失败：{folder}")
        uids = (data[0] or b"").split() if data else []
        if query.startswith("UID "):
            try:
                after = int(query.split(":", 1)[0].split()[1]) - 1
                uids = [uid for uid in uids if int(uid) > after]
            except (IndexError, ValueError):
                pass
        if limit is not None:
            uids = uids[-limit:]
        result: list[tuple[str, bytes]] = []
        for uid in uids:
            status, rows = client.uid("FETCH", uid, "(BODY.PEEK[])")
            if status != "OK":
                uid_text = uid.decode("ascii", errors="replace")
                raise MailAdapterError(f"读取邮件 UID {uid_text} 失败，已保留同步进度")
            raw = next((row[1] for row in rows or [] if isinstance(row, tuple) and isinstance(row[1], bytes)), None)
            uid_text = uid.decode("ascii", errors="strict")
            if not uid_text.isdecimal() or raw is None:
                raise MailAdapterError("QQ 邮箱返回了无法核验的邮件 UID 内容，已保留同步进度")
            result.append((uid_text, raw))
        return result

    def fetch_inbox(self, folder: str, after_uid: int = 0) -> list[RemoteMail]:
        query = f"UID {after_uid + 1}:*" if after_uid else "ALL"
        result: list[RemoteMail] = []
        for uid, raw in self._fetch(folder, query):
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            message_id = str(message.get("Message-ID") or f"<uid-{uid}@qq.meido>").strip()
            in_reply_to = str(message.get("In-Reply-To") or "").strip()
            references = str(message.get("References") or "").strip()
            result.append(RemoteMail(
                uid=uid,
                folder=folder,
                message_id=message_id,
                thread_id=_thread_id(message_id, in_reply_to, references, self.connection.username),
                sender=_decode(message.get("From")),
                reply_to=_decode(message.get("Reply-To")) or _decode(message.get("From")),
                recipients=_decode(message.get("To")),
                subject=_decode(message.get("Subject")),
                received_at=(message.get("Date") or "").strip(),
                body_text=_body(message),
                in_reply_to=in_reply_to,
                references=references,
            ))
        return result

    def sent_samples(self, folder: str, limit: int = 8) -> list[RemoteMail]:
        result: list[RemoteMail] = []
        for uid, raw in self._fetch(folder, "ALL", limit):
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            message_id = str(message.get("Message-ID") or f"<uid-{uid}@qq.meido>").strip()
            result.append(RemoteMail(uid, folder, message_id, _thread_id(message_id, "", "", self.connection.username), _decode(message.get("From")), _decode(message.get("Reply-To")) or _decode(message.get("From")), _decode(message.get("To")), _decode(message.get("Subject")), str(message.get("Date") or ""), _body(message)))
        return result

    def drafts(self, folder: str) -> list[RemoteDraft]:
        result: list[RemoteDraft] = []
        for uid, raw in self._fetch(folder, "ALL"):
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            marker = str(message.get("X-Meido-Draft-ID") or "").strip()
            if not marker:
                continue
            subject, recipients, body = _decode(message.get("Subject")), _decode(message.get("To")), _body(message)
            result.append(RemoteDraft(marker, uid, folder, str(message.get("Message-ID") or ""), subject, recipients, body, _draft_hash(subject, recipients, body, str(message.get("In-Reply-To") or ""), str(message.get("References") or ""))))
        return result

    def append_draft(self, folder: str, *, marker: str, recipients: str, subject: str, body: str, in_reply_to: str = "", references: str = "") -> RemoteDraft:
        client = self._imap or self._connect()
        message = EmailMessage()
        message["From"] = self.connection.username
        message["To"] = recipients
        message["Subject"] = subject
        message["Message-ID"] = f"<meido-{marker}@local.meido>"
        message["X-Meido-Draft-ID"] = marker
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        if references:
            message["References"] = references
        message["Date"] = email.utils.format_datetime(datetime.now(timezone.utc))
        message.set_content(body)
        status, _ = client.append(f'"{folder}"', '(\\Draft)', None, message.as_bytes())
        if status != "OK":
            raise MailAdapterError("QQ 草稿箱写入失败")
        raw = message.as_bytes()
        return RemoteDraft(marker, "", folder, message["Message-ID"], subject, recipients, body, _draft_hash(subject, recipients, body, in_reply_to, references))

    def append_test_draft(self, folder: str, marker: str, recipient: str) -> None:
        client = self._imap or self._connect()
        message = EmailMessage()
        message["From"] = self.connection.username
        message["To"] = recipient
        message["Subject"] = f"[Meido QQ 邮箱测试] {marker}"
        message["Message-ID"] = f"<{marker}@local.meido>"
        message["X-Meido-Test-ID"] = marker
        message.set_content("请在 QQ 邮箱网页端修改这封测试草稿，然后发送给自己。Meido 不会发送邮件。")
        status, _ = client.append(f'"{folder}"', '(\\Draft)', None, message.as_bytes())
        if status != "OK":
            raise MailAdapterError("QQ 草稿箱不可写入")

    def verify_test_sent(self, marker: str, folders: dict[str, str]) -> bool:
        for _uid, raw in self._fetch(folders["sent"], "ALL"):
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            if str(message.get("X-Meido-Test-ID") or "").strip() == marker:
                return True
        return False

    def verify_test_draft_present(self, marker: str, folder: str) -> bool:
        for _uid, raw in self._fetch(folder, "ALL"):
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            if str(message.get("X-Meido-Test-ID") or "").strip() == marker:
                return True
        return False
