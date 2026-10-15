"""Owner-bound pushes and opt-in reminder destinations.

The destination remains a host-owned pairing fact.  Neither a model nor a
reminder can turn a raw platform ID into authorization to contact a stranger.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .host import ChannelHost

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore


class ChannelPushService:
    def __init__(self, store: "MultichannelStore", host: ChannelHost) -> None:
        self.store, self.host = store, host
        with store._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS channel_push_targets (
                    role_id TEXT NOT NULL, account_id TEXT NOT NULL,
                    target_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id,account_id)
                );
                CREATE TABLE IF NOT EXISTS channel_push_events (
                    role_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, target_id TEXT NOT NULL,
                    status TEXT NOT NULL, attempt_id TEXT, error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(role_id,event_id,account_id)
                );
            """)
            db.execute("UPDATE channel_push_events SET status='uncertain',error='process_interrupted' WHERE status='pending'")

    def list_targets(self, role_id: str) -> list[dict[str, object]]:
        with self.store._connect() as db:
            selected = {str(row[0]): str(row[1]) for row in db.execute(
                "SELECT account_id,target_id FROM channel_push_targets WHERE role_id=?", (role_id,),
            )}
        result: list[dict[str, object]] = []
        for account in self.store.list_role_accounts(role_id):
            targets = self.store.owner_targets(role_id, account.account_id)
            for target in targets:
                result.append({
                    "accountId": account.account_id, "channel": account.channel,
                    "accountName": account.display_name, "targetId": target.id,
                    "chatId": target.chat_id,
                    "displayName": target.display_name or target.chat_id,
                    "enabled": selected.get(account.account_id) == target.id,
                    "available": self.store.account_effectively_enabled(account),
                })
        return result

    def configure(self, role_id: str, account_id: str, target_id: str | None) -> None:
        account = self.store.get_account(account_id)
        if account is None or account.role_id != role_id:
            raise KeyError(account_id)
        if target_id is not None and not any(
            target.id == target_id for target in self.store.owner_targets(role_id, account_id)
        ):
            raise ValueError("主动提醒只能绑定该账号已配对的主人私聊")
        with self.store._connect() as db:
            if target_id is None:
                db.execute("DELETE FROM channel_push_targets WHERE role_id=? AND account_id=?", (role_id, account_id))
            else:
                db.execute(
                    "INSERT INTO channel_push_targets VALUES(?,?,?,?) ON CONFLICT(role_id,account_id) DO UPDATE SET target_id=excluded.target_id,updated_at=excluded.updated_at",
                    (role_id, account_id, target_id, datetime.now(timezone.utc).isoformat()),
                )

    def clear_account(self, account_id: str) -> None:
        with self.store._connect() as db:
            db.execute("DELETE FROM channel_push_targets WHERE account_id=?", (account_id,))
            db.execute("DELETE FROM channel_push_events WHERE account_id=?", (account_id,))

    def clear_role(self, role_id: str) -> None:
        with self.store._connect() as db:
            db.execute("DELETE FROM channel_push_targets WHERE role_id=?", (role_id,))
            db.execute("DELETE FROM channel_push_events WHERE role_id=?", (role_id,))

    async def deliver_reminder(self, role_id: str, event_id: str, content: str) -> list[dict[str, object]]:
        """Deliver once to explicitly opted-in targets; uncertain sends never retry."""
        outcomes: list[dict[str, object]] = []
        for destination in self.list_targets(role_id):
            if not destination["enabled"]:
                continue
            account_id, target_id = str(destination["accountId"]), str(destination["targetId"])
            now = datetime.now(timezone.utc).isoformat()
            with self.store._connect() as db:
                claimed = db.execute(
                    "INSERT OR IGNORE INTO channel_push_events(role_id,event_id,account_id,target_id,status,updated_at) VALUES(?,?,?,?,'pending',?)",
                    (role_id, event_id, account_id, target_id, now),
                ).rowcount
            if not claimed:
                continue
            attempt_id, error, status = None, None, "failed"
            try:
                # Recheck the pairing at execution, after the event was claimed.
                if not any(item.id == target_id for item in self.store.owner_targets(role_id, account_id)):
                    raise ValueError("主人投递目标已解绑")
                receipt = await self.host.account_send(
                    role_id, account_id, target_id, content, actor="push",
                    source_session_key=f"role:{role_id}", source_kind=f"todo_reminder:{event_id}",
                )
                attempt_id, error, status = receipt.attempt_id, receipt.error, receipt.status.value
            except Exception as failure:
                error = type(failure).__name__
            with self.store._connect() as db:
                db.execute(
                    "UPDATE channel_push_events SET status=?,attempt_id=?,error=?,updated_at=? WHERE role_id=? AND event_id=? AND account_id=?",
                    (status, attempt_id, error, datetime.now(timezone.utc).isoformat(), role_id, event_id, account_id),
                )
            outcomes.append({"accountId": account_id, "status": status, "attemptId": attempt_id, "error": error})
        return outcomes
