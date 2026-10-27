"""One ephemeral, coalesced tool status and typing lease per Telegram run."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import replace

from ..agent_runtime.events import ToolExecutionStartEvent, ToolExecutionUpdateEvent, ToolExecutionEndEvent
from .delivery import DeliveryAttempt, DeliveryRejected
from .models import DeliveryStatus


class TelegramProgress:
    def __init__(self, message, *, run_id, token_getter, transport, scheduler):
        self.run_id, self.token_getter, self.transport, self.scheduler = run_id, token_getter, transport, scheduler
        self.active = True
        guard = message.authorize_dispatch
        self.message = replace(message, delivery_deadline=scheduler.clock() + 120,
            authorize_dispatch=lambda: self.active and (guard is None or guard()))
        self.sequence = 0
        self.call_id = None
        self.tool_name = ''
        self.pending = None
        self.message_id = None
        self.shown = ''
        self.wake = asyncio.Event()
        self.tasks = (asyncio.create_task(self._typing()), asyncio.create_task(self._status()))

    def feed(self, event):
        if (not self.active or event.run_id != self.run_id or event.sequence <= self.sequence
                or not isinstance(event, (ToolExecutionStartEvent, ToolExecutionUpdateEvent, ToolExecutionEndEvent))):
            return
        self.sequence = event.sequence
        if isinstance(event, ToolExecutionStartEvent):
            self.call_id = event.tool_call_id
            self.tool_name = event.tool_name if re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', event.tool_name) else '工具'
            text = f'正在执行：{self.tool_name}'
        else:
            if event.tool_call_id != self.call_id:
                return
            if isinstance(event, ToolExecutionEndEvent):
                text = f'{"执行失败" if event.result.is_error else "已完成"}：{self.tool_name}'
                self.call_id = None
            else:
                # Only the host-defined MCP connection phase is public. Never
                # forward arguments, raw progress, thinking or result details.
                details = event.result.details
                phase = '连接服务' if isinstance(details, Mapping) and details.get('phase') == 'connecting' else '正在执行'
                text = f'{phase}：{self.tool_name}'
        self.pending = (event.sequence, text)
        self.wake.set()

    async def _status(self):
        try:
            while self.active:
                await self.wake.wait()
                self.wake.clear()
                if self.pending is None or self.pending[1] == self.shown:
                    continue
                sent = None
                async def send(part):
                    nonlocal sent
                    # Read the newest snapshot AFTER the scheduler/retry wait.
                    sent = self.pending
                    text = sent[1]
                    token = self.token_getter()
                    if self.message_id is None:
                        return await self.transport.send_message(token, self.message.target.chat_id, text)
                    return await self.transport.edit_message_text(token, self.message.target.chat_id, self.message_id, text)
                attempt = DeliveryAttempt(self.message, scheduler=self.scheduler, max_attempts=5)
                result = await attempt.run([{'kind': 'preview', 'content': '',
                    'operation': 'edit' if self.message_id else 'send'}], send,
                    receipt_id=lambda response: response.get('message_id'))
                if result.status is not DeliveryStatus.SENT:
                    return  # A failed status never falls back to sending the final answer.
                self.message_id = result.platform_message_id
                self.shown = sent[1]
        except asyncio.CancelledError:
            raise
        except Exception:
            return  # Ephemeral feedback cannot fail the user's conversation.

    async def _typing(self):
        sender = getattr(self.transport, 'send_chat_action', None)
        if not callable(sender) or self.scheduler.active >= self.scheduler.capacity:
            return
        lease = DeliveryAttempt(self.message, scheduler=self.scheduler)
        self.scheduler.active += 1
        try:
            while self.active:
                delay = 0
                for attempt in range(5):
                    await self.scheduler.wait(self.message.target.chat_id, 'typing', lease, delay)
                    try:
                        async with asyncio.timeout(max(0, lease.deadline - self.scheduler.clock())):
                            await sender(self.token_getter(), self.message.target.chat_id, authorize_dispatch=lease.require_current)
                        break
                    except DeliveryRejected as error:
                        if error.recovery != 'rate_limit' or attempt == 4:
                            return
                        delay = max(0, error.retry_after, .5 * 2 ** attempt)
                # Avoid a busy loop even with a zero-interval injected scheduler.
                await asyncio.sleep(max(.05, self.scheduler.intervals['typing']))
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            self.scheduler.active -= 1

    async def close(self):
        self.active = False
        self.pending = None
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
