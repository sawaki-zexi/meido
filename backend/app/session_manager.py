from .models import Message, Role, SessionResponse
from .role_store import RoleStore
from .session_store import SessionStore


class SessionManager:
    def __init__(self, roles: RoleStore, sessions: SessionStore) -> None:
        self.roles = roles
        self.sessions = sessions

    def open_role_session(self, role_id: str) -> SessionResponse:
        if self.roles.get(role_id) is None:
            raise KeyError(role_id)
        session = self.sessions.open_role_session(role_id)
        return SessionResponse(session=session, messages=self.sessions.list_messages(session.sessionKey))

    def role_and_history(self, role_id: str) -> tuple[Role, SessionResponse]:
        role = self.roles.get(role_id)
        if role is None:
            raise KeyError(role_id)
        return role, self.open_role_session(role_id)
