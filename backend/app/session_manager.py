from .chat_attachments import public_attachment
from .models import Message, Role, SessionResponse
from .role_store import RoleStore
from .session_store import SessionStore
from .chat_presentation import run_process, run_metrics, process_target


class SessionManager:
    def __init__(self, roles: RoleStore, sessions: SessionStore) -> None:
        self.roles = roles
        self.sessions = sessions

    def open_role_session(self, role_id: str) -> SessionResponse:
        if self.roles.get(role_id) is None:
            raise KeyError(role_id)
        session = self.sessions.open_role_session(role_id)
        messages = self.sessions.list_messages(session.sessionKey)
        runs = {}
        for message in messages:
            if message.runId:
                runs.setdefault(message.runId, []).append(message)
        return SessionResponse(
            session=session,
            messages=[
                self.present_message(message, runs.get(message.runId, [])) for message in messages
                if message.messageType in {"text", "proactive_reminder", "internal_completion", "desktop_push"}
            ],
        )

    def present_message(self, message: Message, messages=None) -> Message:
        result = public_message(message)
        if message.role == 'assistant' and message.messageType == 'text' and message.runId:
            messages = messages if messages is not None else self.sessions.list_messages(message.sessionKey)
            if message.id == process_target(messages, message.runId):
                result.metadata['process'] = run_process(messages, message.runId)
                result.metadata['turnMetrics'] = run_metrics(messages, message.runId)
        return result

    def role_and_history(self, role_id: str) -> tuple[Role, SessionResponse]:
        role = self.roles.get(role_id)
        if role is None:
            raise KeyError(role_id)
        return role, self.open_role_session(role_id)


def public_message(message: Message) -> Message:
    metadata = dict(message.metadata)
    metadata.pop("thinking", None)
    metadata.pop("content_blocks", None)
    metadata.pop("streamThinking", None)
    for key in ("attachments", "quotedAttachments"):
        attachments = metadata.get(key)
        if isinstance(attachments, list):
            metadata[key] = [
                public_attachment(item) for item in attachments
                if isinstance(item, dict) and item.get("id")
            ]
    return message.model_copy(update={"metadata": metadata})
