from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.app.channels.transports import ChannelTransportError, FeishuWebhookTransport


def encrypted_request(payload):
    # Official lark-oapi wire format: IV followed by AES-CBC ciphertext;
    # PKCS7 plaintext is the UTF-8 JSON body without a length prefix.
    key = hashlib.sha256(b"test-encrypt-key").digest()
    iv = b"0123456789abcdef"
    padder = padding.PKCS7(128).padder()
    raw = json.dumps(payload, ensure_ascii=False).encode()
    padded = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    encrypted = base64.b64encode(iv + cipher.update(padded) + cipher.finalize()).decode()
    body = json.dumps({"encrypt": encrypted}).encode()
    signature = hashlib.sha256(b"12345nonce" + b"test-encrypt-key" + body).hexdigest()
    return body, {"X-Lark-Request-Timestamp": "12345", "X-Lark-Request-Nonce": "nonce", "X-Lark-Signature": signature}


def test_encrypted_webhook_challenge_uses_official_lark_wire_format():
    async def run():
        transport = FeishuWebhookTransport("app", "secret", verification_token="verify", encrypt_key="test-encrypt-key")
        try:
            body, headers = encrypted_request({"type": "url_verification", "token": "verify", "challenge": "挑战内容"})
            assert await transport.handle_webhook(body, headers) == {"challenge": "挑战内容"}
        finally:
            await transport.stop()
    asyncio.run(run())


def test_encrypted_webhook_dispatches_decrypted_event_and_still_checks_token():
    async def run():
        received = []
        transport = FeishuWebhookTransport("app", "secret", verification_token="verify", encrypt_key="test-encrypt-key")
        async def handler(payload):
            received.append(payload)
        await transport.start(handler)
        try:
            payload = {"header": {"event_id": "event-1", "token": "verify"}, "event": {"message": {"content": "消息"}}}
            body, headers = encrypted_request(payload)
            assert await transport.handle_webhook(body, headers) == {"code": 0}
            assert received == [payload]
            body, headers = encrypted_request({"header": {"token": "wrong"}})
            with pytest.raises(ChannelTransportError, match="token"):
                await transport.handle_webhook(body, headers)
            assert received == [payload]
        finally:
            await transport.stop()
    asyncio.run(run())
