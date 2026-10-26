"""One bounded logical delivery, with immediately recorded physical receipts."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
import math
import time

from .base import ChannelAdapterError, PartialDeliveryError, PlatformRejectedError, delivery_status_for_error
from .models import DeliveryResult, DeliveryStatus


class DeliveryRejected(PlatformRejectedError):
    def __init__(self, reason, *, recovery=None, retry_after=0):
        super().__init__(reason)
        self.reason, self.recovery = reason, recovery
        self.retry_after = float(retry_after) if isinstance(retry_after, (int, float)) and math.isfinite(retry_after) else 0


class DeliveryStopped(ChannelAdapterError):
    pass


class ReceiptPersistenceError(ChannelAdapterError):
    """Stop the tail without erasing a receipt already returned by the platform."""


_dispatch_guard = ContextVar('channel_delivery_dispatch_guard', default=None)


def check_dispatch():
    """Transports call this after token/connection waits and before their POST."""
    check = _dispatch_guard.get()
    if check is not None:
        check()


def telegram_response(response):
    """Decode the Bot API's explicit rejection, without leaking token URLs."""
    if response.status_code >= 500 or response.status_code == 408:
        response.raise_for_status()
    if response.status_code in {401, 403}:
        raise DeliveryRejected('telegram_permission_denied')
    payload = response.json()
    if isinstance(payload, dict) and payload.get('ok') is False:
        code = payload.get('error_code', response.status_code)
        if isinstance(code, int) and code >= 500:
            raise PartialDeliveryError('telegram_server_error')
        description = str(payload.get('description', '')).lower()
        if code == 429:
            raise DeliveryRejected('telegram_rate_limit', recovery='rate_limit',
                retry_after=payload.get('parameters', {}).get('retry_after', 0))
        if code == 400 and description in {
            'bad request: message to be replied not found',
            'bad request: replied message not found',
        }:
            raise DeliveryRejected('telegram_quote_missing', recovery='quote_missing')
        if code == 400 and ("can't parse entities" in description or 'entity offset' in description):
            raise DeliveryRejected('telegram_format_rejected', recovery='format_rejected')
        if code == 400 and 'message is not modified' in description:
            return {'not_modified': True}
        raise DeliveryRejected(f'telegram_rejected_{code}')
    response.raise_for_status()
    if not isinstance(payload, dict) or payload.get('ok') is not True:
        raise PartialDeliveryError('telegram_invalid_response')
    return payload.get('result') or {}


class OutboundScheduler:
    def __init__(self, *, send_interval=0, edit_interval=0, typing_interval=0, account_interval=0,
                 capacity=64, clock=time.monotonic, sleep=asyncio.sleep):
        self.intervals = {'send': send_interval, 'edit': edit_interval, 'typing': typing_interval}
        self.account_interval, self.capacity = account_interval, capacity
        self.clock, self.sleep = clock, sleep
        self.active, self.account_next = 0, 0.0
        self.chat_next = OrderedDict()

    async def wait(self, chat_id, kind, attempt, delay=0):
        not_before = self.clock() + delay
        key = (chat_id, 'typing' if kind == 'typing' else 'message')
        while True:
            attempt.require_current()
            now = self.clock()
            ready = max(not_before, self.account_next, self.chat_next.get(key, 0))
            if ready >= attempt.deadline:
                raise DeliveryStopped('delivery_deadline')
            if ready > now:
                await self.sleep(ready - now)
                continue
            # Reserve only at dispatch. A rate-limited chat never reserves the
            # account's future slots and blocks otherwise-ready chats.
            self.account_next = now + self.account_interval
            self.chat_next[key] = now + self.intervals[kind]
            self.chat_next.move_to_end(key)
            for previous in tuple(self.chat_next):
                if self.chat_next[previous] <= now:
                    self.chat_next.pop(previous)
            return


class DeliveryAttempt:
    def __init__(self, message, *, scheduler=None, max_attempts=1, retry_base=.5):
        self.message = message
        self.scheduler = scheduler or OutboundScheduler()
        self.max_attempts, self.retry_base = max_attempts, retry_base
        self.deadline = min(self.scheduler.clock() + 120,
            message.delivery_deadline if message.delivery_deadline is not None else float('inf'))
        self.deadline_at = time.time() + max(0, self.deadline - self.scheduler.clock())
        self.parts = []

    def require_current(self):
        if self.scheduler.clock() >= self.deadline:
            raise DeliveryStopped('delivery_deadline')
        if self.message.authorize_dispatch is not None and not self.message.authorize_dispatch():
            raise DeliveryStopped('source_revoked')

    def details(self):
        sent = [part for part in self.parts if part.get('platformMessageId') and (
            part['status'] == 'sent' or part.get('confirmedContent') is not None)]
        return {'parts': deepcopy(self.parts), 'deliveryDeadlineAt': self.deadline_at,
            'streaming': self.message.metadata.get('streamingDelivery') is True,
            'targetId': self.message.target.id, 'messageThreadId': self.message.target.metadata.get('message_thread_id'),
            'requestedReplyTo': self.message.reply_to_message_id,
            'partialDelivery': bool(sent) and any(part['status'] != 'sent' for part in self.parts),
            'deliveredContent': ''.join(part['content'] if part['status'] == 'sent' else part['confirmedContent']
                for part in sent if part['kind'] != 'preview'),
            'receiptKind': 'platform_message',
            'platform_message_ids': [part['platformMessageId'] for part in sent]}

    def publish(self):
        if self.message.record_delivery is not None:
            try:
                self.message.record_delivery(self.details())
            except Exception as error:
                raise ReceiptPersistenceError('receipt_persistence_failed') from error

    async def run(self, parts, send, *, receipt_id):
        if not parts or len(parts) > 128:
            raise ChannelAdapterError('invalid_delivery_parts')
        self.parts = [{**part, 'index': index, 'status': 'pending', 'attempts': 0,
            'platformMessageId': part.get('platformMessageId'), 'error': None, 'withoutReply': False, 'plainText': False}
            for index, part in enumerate(parts)]
        self.publish()
        if self.scheduler.active >= self.scheduler.capacity:
            self.parts[0].update(status='failed', error='delivery_queue_full')
            self.publish()
            return self.result()
        self.scheduler.active += 1
        try:
            for part in self.parts:
                delay = 0
                while part['attempts'] < self.max_attempts:
                    dispatched = False
                    try:
                        await self.scheduler.wait(self.message.target.chat_id, part.get('operation', 'send'), self, delay)
                        part['attempts'] += 1
                        part['status'] = 'sending'
                        part['error'] = None
                        self.publish()
                        token = _dispatch_guard.set(self.require_current)
                        try:
                            dispatched = True
                            async with asyncio.timeout(max(0, self.deadline - self.scheduler.clock())):
                                response = await send(part)
                        finally:
                            _dispatch_guard.reset(token)
                        identifier = receipt_id(response)
                        if not identifier:
                            raise PartialDeliveryError('missing_platform_receipt')
                        part.update(status='sent', platformMessageId=str(identifier))
                        self.publish()
                        break
                    except ReceiptPersistenceError:
                        # The host still attempts to persist the final snapshot.
                        # Never turn a known receipt into a reason to resend it.
                        if part['status'] != 'sent':
                            part.update(status='failed', error='receipt_persistence_failed')
                        return self.result(error='receipt_persistence_failed')
                    except asyncio.CancelledError:
                        part.update(status='uncertain' if dispatched else 'failed', error='dispatch_cancelled')
                        self.publish()
                        raise
                    except Exception as error:
                        recovery = error.recovery if isinstance(error, DeliveryRejected) else None
                        reason = error.reason if isinstance(error, DeliveryRejected) else str(error) if isinstance(error, DeliveryStopped) else type(error).__name__
                        retry = part['attempts'] < self.max_attempts
                        if recovery == 'rate_limit' and retry:
                            delay = max(0, error.retry_after, self.retry_base * 2 ** (part['attempts'] - 1))
                        elif recovery == 'quote_missing' and retry and self.message.reply_to_message_id and not part['withoutReply']:
                            part['withoutReply'], delay = True, 0
                        elif recovery == 'format_rejected' and retry and not part['plainText']:
                            part['plainText'], delay = True, 0
                        else:
                            status = DeliveryStatus.FAILED if not dispatched or isinstance(error, DeliveryStopped) else delivery_status_for_error(error)
                            if dispatched and not isinstance(error, (ChannelAdapterError,)) and getattr(error, 'response', None) is None:
                                status = DeliveryStatus.UNCERTAIN
                            part.update(status=status.value, error=reason)
                            self.publish()
                            return self.result()
                        part.update(status='pending', error=reason)
                        self.publish()
        finally:
            self.scheduler.active -= 1
        return self.result()

    def result(self, *, error=None):
        details = self.details()
        statuses = {part['status'] for part in self.parts}
        status = (DeliveryStatus.SENT if statuses == {'sent'} and error is None else
            DeliveryStatus.UNCERTAIN if statuses & {'uncertain', 'sending'} else DeliveryStatus.FAILED)
        return DeliveryResult(status, self.message.target,
            next(iter(details['platform_message_ids']), None),
            error or next((part['error'] for part in self.parts if part['error']), None), raw=details)


class StreamDelivery:
    """One original stream lease; final text replaces the confirmed preview.

    Updates are separate edit operations (each with a bounded rejection retry),
    sharing the original deadline, account scheduler and durable receipt row.
    An uncertain edit stops the stream; it never starts a new plain message.
    """
    def __init__(self, message, scheduler, *, max_attempts):
        self.message = replace(message, delivery_deadline=min(scheduler.clock() + 120,
            message.delivery_deadline if message.delivery_deadline is not None else float('inf')))
        self.scheduler, self.max_attempts = scheduler, max_attempts
        self.message_id, self.confirmed, self.error = None, '', None
        self.last_result = None

    def require_current(self):
        DeliveryAttempt(self.message, scheduler=self.scheduler).require_current()

    async def update(self, content, send, *, receipt_id, kind='edit', preview=False):
        if self.error is not None:
            return self.last_result
        parts = [{'kind': 'preview' if preview else 'text', 'content': content, 'operation': kind,
            'platformMessageId': self.message_id,
            'confirmedContent': self.confirmed if self.message_id else None}]
        message = replace(self.message, metadata={**self.message.metadata, 'streamingDelivery': True})
        result = await DeliveryAttempt(message, scheduler=self.scheduler, max_attempts=self.max_attempts).run(
            parts, lambda part: send(part), receipt_id=receipt_id)
        self.last_result = result
        if result.status is DeliveryStatus.SENT:
            self.message_id = result.platform_message_id
            self.confirmed = '' if preview else content
        else:
            self.error = result.error or 'stream_failed'
        return result

    async def finish(self, parts, send, *, receipt_id):
        if self.error is not None:
            return self.last_result
        if self.message_id:
            parts[0].update(platformMessageId=self.message_id, confirmedContent=self.confirmed, operation='edit')
        result = await DeliveryAttempt(self.message, scheduler=self.scheduler, max_attempts=self.max_attempts).run(
            parts, send, receipt_id=receipt_id)
        self.last_result = result
        return result
