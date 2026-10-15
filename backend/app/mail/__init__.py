"""QQ mailbox integration and draft processing."""

from .adapter import QQMailAdapter, MailAdapterError, MailConnection
from .service import MailService
from .store import MailStore

__all__ = ["QQMailAdapter", "MailAdapterError", "MailConnection", "MailService", "MailStore"]
