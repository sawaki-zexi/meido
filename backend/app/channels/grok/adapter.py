from __future__ import annotations

import asyncio
import json
import os
import subprocess
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

import httpx

from ..base import ChannelAdapter, ChannelAdapterError, PartialDeliveryError, PlatformRejectedError, UnsupportedTargetError
from ..delivery import DeliveryAttempt, OutboundScheduler, check_dispatch
from ..models import ChannelCapabilities, ChatType, DeliveryResult, DeliveryStatus, InboundEvent, TargetKind, target_from_chat
from .callback import CallbackServer
from .config import PROTOCOL, client_paths
from .exchange import ExchangeJournal
from .protocol import BotMessage, CallbackError


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'nt':
        # Restrict before writing any credential. Only the path enters the process arguments.
        quoted = str(path).replace("'", "''")
        script = ("$ErrorActionPreference='Stop'; $acl=[System.Security.AccessControl.DirectorySecurity]::new(); "
                  "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; "
                  "$acl.SetOwner($sid); $acl.SetAccessRuleProtection($true,$false); "
                  "$acl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new("
                  "$sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')); "
                  f"[System.IO.Directory]::SetAccessControl('{quoted}', $acl)")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ChannelAdapterError('无法保护 Grok 本机连接文件')
    else:
        path.chmod(0o700)


class GrokAdapter(ChannelAdapter):
    channel = 'grok_bot'
    capabilities = ChannelCapabilities(private=True, replies=True)

    def __init__(self, account_id, role_id, settings, database_path, *, webhook_key, callback_token,
                 can_receive=lambda: True, http_transport=None):
        super().__init__()
        self.account_id, self.role_id = account_id, role_id
        self.settings = dict(settings)
        self.peer_id = settings['peerId']
        self.webhook_key, self.callback_token = webhook_key, callback_token
        self.can_receive, self.http_transport = can_receive, http_transport
        self.client_path, self.client_file = client_paths(database_path, account_id, self.peer_id)
        self.journal = ExchangeJournal(database_path, role_id, account_id, self.peer_id)
        self.target = target_from_chat(account_id=account_id, chat_id='dm:' + self.peer_id,
                                      kind=TargetKind.PRIVATE, display_name='Grok Bot')
        self._targets = {self.target.chat_id: self.target}
        self.scheduler = OutboundScheduler()
        self.callback = CallbackServer(callback_token, self.receive)
        self.client = None
        self.worker = None
        self.wake = asyncio.Event()
        self.active = False
        self.observed_at = None

    async def start(self):
        if self.active:
            return
        await asyncio.to_thread(_private_directory, self.client_file.parent)
        try:
            await self.callback.start()
            self.client = httpx.AsyncClient(transport=self.http_transport, timeout=15, follow_redirects=False)
            payload = {'protocol': PROTOCOL, 'callback_url': f'http://127.0.0.1:{self.callback.port}/',
                       'token': self.callback_token}
            descriptor = os.open(self.client_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                json.dump(payload, file)
            self.active = True
            self.observed_at = datetime.now(timezone.utc).isoformat()
            self.worker = asyncio.create_task(self._receive_pending())
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.active = False
        await self.callback.stop()
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        for message in self.journal.pending():
            self.journal.finish(message.message_id, 'failed')
        if self.client is not None:
            await self.client.aclose()
            self.client = None
        self.client_file.unlink(missing_ok=True)

    def resume_intake(self):
        self.wake.set()

    def connection_snapshot(self):
        return {'connection': 'connected' if self.active else 'disconnected', 'online': None,
                'observedAt': self.observed_at, 'reason': 'local_callback_ready' if self.active else 'stopped'}

    async def list_targets(self):
        return [self.target]

    def remember_target(self, target):
        if target.id != self.target.id or target.account_id != self.account_id or target.chat_id != self.target.chat_id:
            raise UnsupportedTargetError('此目标不属于当前 Grok Bot 连接')

    def parse_event(self, payload):
        message = BotMessage.parse(dict(payload))
        return InboundEvent(channel=self.channel, account_id=self.account_id,
            external_event_id=f'{self.account_id}:{self.peer_id}:{message.message_id}',
            external_message_id=message.message_id, chat_id=self.target.chat_id,
            chat_type=ChatType.PRIVATE, sender_id=self.peer_id, sender_name='Grok Bot',
            content=message.text, reply_to_message_id=message.reply_to or None,
            metadata={'grokProtocol': PROTOCOL})

    async def receive(self, payload):
        if not self.active or not self.can_receive():
            raise CallbackError(409, '连接已暂停或正在切换')
        message = BotMessage.parse(payload)
        fresh, receipt = self.journal.begin(message, 'in', self.settings['maxMessages'])
        if fresh:
            self.wake.set()
        return (202 if fresh else 200), {'status': 'accepted' if fresh else 'duplicate',
                                        'message_id': message.message_id, 'state': receipt['status']}

    async def _receive_pending(self):
        while self.active:
            await self.wake.wait()
            self.wake.clear()
            for message in self.journal.pending():
                if not self.active:
                    return
                if not self.can_receive():
                    self.journal.finish(message.message_id, 'failed')
                    continue
                # A channel turn is cancellable by the runtime's Stop action.
                # Keep that cancellation below the intake worker so one
                # stopped turn cannot silently kill the callback receiver.
                delivery = asyncio.create_task(self._emit_payload(asdict(message)))
                try:
                    await delivery
                except asyncio.CancelledError:
                    if not self.active:
                        raise
                    self.journal.finish(message.message_id, 'failed')
                    continue
                except Exception:
                    self.journal.finish(message.message_id, 'failed')
                    continue
                self.journal.finish(message.message_id, 'complete')

    async def send(self, message):
        self.remember_target(message.target)
        if message.attachments or message.metadata.get('mentionIds'):
            raise UnsupportedTargetError('Grok Bot 仅支持文字私聊')
        identifier = 'meido:' + str(message.metadata.get('deliveryAttemptId') or uuid.uuid4().hex)
        try:
            bot_message = BotMessage.parse({'message_id': identifier, 'text': message.content,
                                           'reply_to': message.reply_to_message_id or ''})
        except CallbackError as error:
            return DeliveryResult(DeliveryStatus.FAILED, message.target, error=error.reason)
        attempt = DeliveryAttempt(message, scheduler=self.scheduler, transport_acceptance=True)

        async def post(_part):
            if not self.active or self.client is None:
                raise ChannelAdapterError('Grok 本机入口尚未启用')
            attempt.require_current()
            try:
                fresh, receipt = self.journal.begin(bot_message, 'out', self.settings['maxMessages'])
            except CallbackError as error:
                raise PlatformRejectedError(error.reason) from None
            if not fresh:
                if receipt['status'] == 'accepted':
                    return {'httpStatus': 200}
                raise PartialDeliveryError('原发送结果未知或失败，不能自动重放')
            dispatched = False
            try:
                check_dispatch()
                payload = {**asdict(bot_message), 'protocol': PROTOCOL, 'type': 'message',
                           'exchange_id': receipt['root'], 'role_id': self.role_id,
                           'callback_client': str(self.client_path), 'connection_file': str(self.client_file)}
                dispatched = True
                async with self.client.stream('POST', self.settings['endpoint'], json=payload,
                        headers={'Authorization': 'Bearer ' + self.webhook_key}) as response:
                    status = response.status_code
                    if status == 200:
                        self.journal.finish(identifier, 'accepted')
                        return {'httpStatus': 200}
                    if status >= 500 or status == 408:
                        raise PartialDeliveryError('Grok Webhook 接收结果不确定')
                    raise PlatformRejectedError(f'Grok Webhook 拒绝请求（HTTP {status}）')
            except BaseException as error:
                uncertain = dispatched and not isinstance(error, PlatformRejectedError)
                self.journal.finish(identifier, 'uncertain' if uncertain else 'failed')
                if isinstance(error, (asyncio.CancelledError, ChannelAdapterError)):
                    raise
                raise PartialDeliveryError('Grok Webhook 接收结果不确定') from None

        return await attempt.run([{'kind': 'text', 'content': message.content}], post, receipt_id=lambda _: None)
