"""QQ mailbox integration and draft processing."""

from .adapter import QQMailAdapter, MailAdapterError, MailConnection
from .service import MailModelCompletion, MailService
from .store import MailStore

__all__ = ["QQMailAdapter", "MailAdapterError", "MailConnection", "MailModelCompletion", "MailService", "MailStore"]
