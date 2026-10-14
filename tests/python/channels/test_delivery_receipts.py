from __future__ import annotations

import asyncio

import pytest

from backend.app.channels.models import DeliveryStatus, OutboundMessage, TargetKind, target_from_chat
from backend.app.channels.qqbot import QQBotAdapter, QQBotConfig
from backend.app.channels.telegram import TelegramAccountAdapter


class MissingReceiptTransport:
    async def __call__(self, operation, params):
        return {}

    async def send_message(self, token, chat_id, text, **options):
        return {}


@pytest.mark.parametrize("channel", ["telegram", "qqbot"])
def test_plain_send_without_real_message_receipt_remains_uncertain(channel):
    async def scenario():
        transport = MissingReceiptTransport()
        adapter = (
            TelegramAccountAdapter("account", lambda: "secret", transport)
            if channel == "telegram" else QQBotAdapter(QQBotConfig(account_id="account", app_id="app"), transport=transport)
        )
        target = target_from_chat(account_id="account", chat_id="user", kind=TargetKind.PRIVATE)
        adapter.remember_target(target)
        result = await adapter.send(OutboundMessage(target, "hello"))

        assert result.status is DeliveryStatus.UNCERTAIN
        assert result.platform_message_id is None

    asyncio.run(scenario())


def test_qqbot_stream_without_real_receipt_remains_uncertain():
    async def scenario():
        adapter = QQBotAdapter(QQBotConfig(account_id="account", app_id="app"), transport=MissingReceiptTransport())
        target = target_from_chat(account_id="account", chat_id="user", kind=TargetKind.PRIVATE)
        adapter.remember_target(target)
        session = await adapter.stream_start(target, reply_to_message_id="trigger")
        await adapter.stream_update(session, "preview")
        result = await adapter.stream_finalize(session, "final")

        assert result.status is DeliveryStatus.UNCERTAIN
        assert result.platform_message_id is None

    asyncio.run(scenario())
