from __future__ import annotations

import email
import email.policy
import hashlib
from html import unescape
import imaplib
import re
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from .models import MailConnection
from .date import mail_date_key


class MailAdapterError(RuntimeError):
    """A safe, user-facing mailbox operation error."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


READ_RETRY_ATTEMPTS = 3
READ_RETRY_BASE_DELAY_SECONDS = 0.2
_TRANSIENT_READ_ERRORS = (OSError, TimeoutError, socket.timeout, imaplib.IMAP4.abort)


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
    draft_marker: str = ""
    uid_validity: str = ""


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


def _test_marker_matches(message: email.message.Message, marker: str) -> bool:
    if str(message.get("X-Meido-Test-ID") or "").strip() == marker:
        return True
    if str(message.get("Message-ID") or "").strip() == f"<{marker}@local.meido>":
        return True
    return marker in _decode(message.get("Subject"))


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return "\n".join(line.strip() for line in value.splitlines()).strip()


def _body(message: email.message.Message) -> str:
    if message.is_multipart():
        parts = list(message.walk())
        plain = [item for item in parts if item.get_content_type() == "text/plain" and not item.get_filename()]
        html = [item for item in parts if item.get_content_type() == "text/html" and not item.get_filename()]
        candidates = plain or html
        value = "\n".join(str(item.get_content()) for item in candidates[:3]).strip()
        if candidates and candidates[0].get_content_type() == "text/html":
            value = _html_to_text(value)
        return re.sub(r"[ \t]+", " ", value).strip()
    if message.get_content_type() == "text/html":
        return _html_to_text(str(message.get_content() or ""))
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
        self._folder_uid_validity: dict[str, str] = {}

    @staticmethod
    def _read_uid_validity(client: object) -> str:
        response = getattr(client, "response", None)
        if not callable(response):
            return ""
        try:
            _status, values = response("UIDVALIDITY")
        except (AttributeError, OSError, imaplib.IMAP4.error, TypeError, ValueError):
            return ""
        if not values:
            return ""
        value = values[0] if isinstance(values, (list, tuple)) else values
        if isinstance(value, bytes):
            value = value.decode("ascii", errors="ignore")
        return str(value or "").strip().strip('"')

    def _drop_connection(self) -> None:
        """Discard a broken read connection without retrying any write."""
        client = self._imap
        self._imap = None
        self._folder_uid_validity.clear()
        if client is None:
            return
        try:
            client.logout()
        except (AttributeError, OSError, imaplib.IMAP4.error):
            pass

    def _read_with_retry(self, operation, *, error_message: str):
        """Run a read-only IMAP operation with bounded reconnects.

        The operation must be idempotent and receive the currently connected
        client.  Callers use this only for SELECT/LIST/SEARCH/FETCH; APPEND is
        deliberately kept outside this helper because a lost APPEND response
        must be reconciled by marker, never repeated blindly.
        """
        last_error: BaseException | None = None
        for attempt in range(READ_RETRY_ATTEMPTS):
            try:
                client = self._imap or self._connect()
                return operation(client)
            except MailAdapterError as error:
                if not bool(getattr(error, "transient", False)):
                    raise
                last_error = error
            except _TRANSIENT_READ_ERRORS as error:
                last_error = error
            if attempt + 1 >= READ_RETRY_ATTEMPTS:
                raise MailAdapterError(error_message, transient=True) from last_error
            self._drop_connection()
            time.sleep(READ_RETRY_BASE_DELAY_SECONDS * (2 ** attempt))
        raise MailAdapterError(error_message, transient=True) from last_error

    def _select_on_client(self, client: object, folder: str) -> object:
        status, data = client.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise MailAdapterError(f"QQ 邮箱文件夹不可访问：{folder}")
        self._folder_uid_validity[folder] = self._read_uid_validity(client)
        return data

    def _select_readonly(self, folder: str) -> tuple[object, object]:
        data = self._read_with_retry(
            lambda client: self._select_on_client(client, folder),
            error_message=f"读取 QQ 邮箱文件夹暂时失败：{folder}",
        )
        client = self._imap or self._connect()
        return client, data

    def folder_uid_validity(self, folder: str) -> str | None:
        """Read a folder's IMAP UIDVALIDITY without changing mailbox state."""
        self._select_readonly(folder)
        value = self._folder_uid_validity.get(folder, "")
        return value or None

    def _connect(self) -> imaplib.IMAP4_SSL:
        if not self.connection.use_ssl:
            raise MailAdapterError("QQ 邮箱仅允许使用 SSL IMAP 连接")
        try:
            client = imaplib.IMAP4_SSL(self.connection.host, self.connection.port, timeout=self.timeout)
            client.login(self.connection.username, self.password)
        except imaplib.IMAP4.abort as error:
            raise MailAdapterError("QQ 邮箱连接被远端中断，请稍后重试", transient=True) from error
        except imaplib.IMAP4.error as error:
            detail = str(error).casefold()
            if any(token in detail for token in ("auth", "login", "password", "credential", "fail")):
                raise MailAdapterError("QQ 邮箱认证失败，请确认已开启 IMAP 服务并使用邮箱授权码") from error
            raise MailAdapterError("QQ 邮箱拒绝了 IMAP 请求，请检查邮箱服务设置") from error
        except socket.gaierror as error:
            raise MailAdapterError("无法解析 QQ 邮箱 IMAP 地址，请检查网络 DNS", transient=True) from error
        except (TimeoutError, socket.timeout) as error:
            raise MailAdapterError("连接 QQ 邮箱超时，请检查网络后重试", transient=True) from error
        except OSError as error:
            raise MailAdapterError("无法连接 QQ 邮箱，请检查网络和 993 端口访问", transient=True) from error
        self._imap = client
        return client

    def close(self) -> None:
        self._drop_connection()

    def folders(self) -> dict[str, str]:
        try:
            status, rows = self._read_with_retry(
                lambda client: client.list(),
                error_message="读取 QQ 邮箱文件夹暂时失败，请稍后重试",
            )
            if status != "OK":
                raise MailAdapterError("QQ 邮箱文件夹列表不可访问")
        except imaplib.IMAP4.error as error:
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
        folders = self.folders()
        counts: dict[str, int] = {}
        try:
            for key, folder in folders.items():
                status, data = self._read_with_retry(
                    lambda client, folder=folder: client.select(f'"{folder}"', readonly=True),
                    error_message=f"读取 QQ 邮箱文件夹暂时失败：{key}",
                )
                if status != "OK":
                    raise MailAdapterError(f"QQ 邮箱文件夹不可访问：{key}")
                counts[key] = int((data or [b"0"])[0] or b"0")
        except (OSError, imaplib.IMAP4.error, ValueError) as error:
            if isinstance(error, MailAdapterError):
                raise
            raise MailAdapterError("QQ 邮箱文件夹不可访问") from error
        result: dict[str, object] = {"folders": folders, "counts": counts, "testDraft": None}
        if write_test:
            client = self._imap or self._connect()
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

    def _fetch(
        self,
        folder: str,
        query: str = "ALL",
        limit: int | None = None,
        *,
        newest_first: bool = True,
    ) -> list[tuple[str, bytes]]:
        def operation(client: object) -> list[tuple[str, bytes]]:
            self._select_on_client(client, folder)
            status, data = client.uid("SEARCH", None, query)
            if status != "OK":
                raise MailAdapterError(f"QQ 邮箱搜索失败：{folder}")
            uids = (data[0] or b"").split() if data else []
            # IMAP servers normally return SEARCH results in UID order, but the
            # provider contract does not require callers to rely on that order.
            # Sorting before applying a bounded tail keeps the newest messages in
            # the window even when a server returns the result set differently.
            uids = sorted(uids, key=lambda value: int(value) if value.isdigit() else -1)
            if query.startswith("UID "):
                try:
                    after = int(query.split(":", 1)[0].split()[1]) - 1
                    uids = [uid for uid in uids if int(uid) > after]
                except (IndexError, ValueError):
                    pass
            if limit is not None:
                bounded_limit = max(0, int(limit))
                if bounded_limit == 0:
                    return []
                uids = uids[-bounded_limit:] if newest_first else uids[:bounded_limit]
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

        return self._read_with_retry(
            operation,
            error_message=f"读取 QQ 邮箱邮件暂时失败：{folder}",
        )

    def fetch_inbox(self, folder: str, after_uid: int = 0, limit: int | None = None) -> list[RemoteMail]:
        query = f"UID {after_uid + 1}:*" if after_uid else "ALL"
        result: list[RemoteMail] = []
        fetched = self._fetch(folder, query) if limit is None else self._fetch(folder, query, limit, newest_first=False)
        for uid, raw in fetched:
            uid_validity = self._folder_uid_validity.get(folder, "")
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            fallback_id = f"<uid-{uid_validity}-{uid}@qq.meido>" if uid_validity else f"<uid-{uid}@qq.meido>"
            message_id = str(message.get("Message-ID") or fallback_id).strip()
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
                uid_validity=uid_validity,
            ))
        return result

    def fetch_sent(self, folder: str, after_uid: int = 0, limit: int | None = None) -> list[RemoteMail]:
        """Read sent copies incrementally without sending or mutating the mailbox."""
        query = f"UID {after_uid + 1}:*" if after_uid else "ALL"
        result: list[RemoteMail] = []
        fetched = self._fetch(folder, query) if limit is None else self._fetch(folder, query, limit, newest_first=after_uid == 0)
        for uid, raw in fetched:
            uid_validity = self._folder_uid_validity.get(folder, "")
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            fallback_id = f"<sent-uid-{uid_validity}-{uid}@qq.meido>" if uid_validity else f"<sent-uid-{uid}@qq.meido>"
            message_id = str(message.get("Message-ID") or fallback_id).strip()
            in_reply_to = str(message.get("In-Reply-To") or "").strip()
            references = str(message.get("References") or "").strip()
            recipient_headers = [
                str(message.get(header))
                for header in ("To", "Cc", "Bcc")
                if message.get(header)
            ]
            recipients = ", ".join(_decode(header) for header in recipient_headers)
            result.append(RemoteMail(
                uid=uid,
                folder=folder,
                message_id=message_id,
                thread_id=_thread_id(message_id, in_reply_to, references, self.connection.username),
                sender=_decode(message.get("From")),
                # A sent copy without Reply-To must fall back to its actual
                # recipients at the follow-up layer.  Using From here makes
                # the owner look like the recipient and can suppress a valid
                # follow-up target after self-address filtering.
                reply_to=_decode(message.get("Reply-To")),
                recipients=recipients,
                subject=_decode(message.get("Subject")),
                received_at=str(message.get("Date") or ""),
                body_text=_body(message),
                in_reply_to=in_reply_to,
                references=references,
                draft_marker=str(message.get("X-Meido-Draft-ID") or "").strip(),
                uid_validity=uid_validity,
            ))
        return result

    def sent_samples(self, folder: str, limit: int = 8) -> list[RemoteMail]:
        return self.fetch_sent(folder, after_uid=0, limit=max(1, min(limit, 24)))

    def related_sent(self, folder: str, participant: str, limit: int = 24) -> list[RemoteMail]:
        participant = participant.strip().casefold()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", participant):
            raise MailAdapterError("邮件对象地址无效")
        requested = max(1, min(limit, 24))
        # IMAP search results can include malformed or group-addressed headers.
        # Read a bounded candidate window so local recipient validation can still
        # return the requested number of valid samples.
        fetch_limit = min(96, max(requested, requested * 3))
        result: list[RemoteMail] = []
        query = f'OR OR TO "{participant}" CC "{participant}" BCC "{participant}"'
        for uid, raw in self._fetch(folder, query, fetch_limit, newest_first=True):
            uid_validity = self._folder_uid_validity.get(folder, "")
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            recipient_headers = [str(message.get(header)) for header in ("To", "Cc", "Bcc") if message.get(header)]
            if participant not in {address.casefold() for _, address in email.utils.getaddresses(recipient_headers)}:
                continue
            recipients = ", ".join(_decode(header) for header in recipient_headers)
            fallback_id = f"<sent-uid-{uid_validity}-{uid}@qq.meido>" if uid_validity else f"<sent-uid-{uid}@qq.meido>"
            message_id = str(message.get("Message-ID") or fallback_id).strip()
            parent = str(message.get("In-Reply-To") or "").strip()
            references = str(message.get("References") or "").strip()
            result.append(RemoteMail(uid, folder, message_id,
                _thread_id(message_id, parent, references, self.connection.username),
                _decode(message.get("From")), _decode(message.get("Reply-To")),
                recipients, _decode(message.get("Subject")), str(message.get("Date") or ""), _body(message), parent, references,
                uid_validity=uid_validity))
        # IMAP search order is provider-defined and the candidate window can
        # contain malformed or duplicate Message-IDs.  Filter and sort the
        # complete bounded window before taking the requested number so a
        # recent valid reply is not displaced by older candidates.
        unique: dict[str, RemoteMail] = {}
        for row in result:
            key = row.message_id or f"uid:{row.uid}"
            previous = unique.get(key)
            if previous is None or mail_date_key(row.received_at, row.message_id, row.uid) > mail_date_key(previous.received_at, previous.message_id, previous.uid):
                unique[key] = row
        return sorted(unique.values(), key=lambda row: mail_date_key(row.received_at, row.message_id, row.uid), reverse=True)[:requested]

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
            if _test_marker_matches(message, marker):
                return True
        return False

    def verify_test_draft_present(self, marker: str, folder: str) -> bool:
        for _uid, raw in self._fetch(folder, "ALL"):
            message = BytesParser(policy=email.policy.default).parsebytes(raw)
            if _test_marker_matches(message, marker):
                return True
        return False
