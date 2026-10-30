"""Model-authored chat messages, independent of business-tool permissions.

Adapted from Shiori's natural_reply/reply_sequence contract (d3acaed).
MIT attribution: third_party/shiori-agent/LICENSE. This is ordinary reply
formatting, not the separate optional unaddressed-group participation worker.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, replace

from .provider import AssistantDoneEvent, ProviderErrorEvent, ProviderStartEvent, ThinkingDeltaEvent, TextDeltaEvent, ToolCallEndEvent
from .types import AssistantMessage, ToolResultMessage, UserMessage

FINISH_REPLY = "finish_reply"
NATURAL_CHAT_PROMPT = (
    "## 即时聊天分条提交\n条数、分条位置和结束时机由你根据当前对话决定。"
    "一条消息足够时就发一条，同一条消息内部可以正常换行。\n"
    "正式回复通过 finish_reply 的 messages 列表提交，每项是一条实际要发送的独立消息；"
    "不要在普通正文里另发一份。需要业务工具时先获取结果，再单独完成回复。"
    "发送协议不属于聊天话题，无需向对方解释。"
)


def natural_chat_enabled(role) -> bool:
    """Only an explicit role-record boolean opts into the message protocol."""
    return role is not None and getattr(role.agentConfig, 'naturalChatEnabled', False) is True


def finish_reply_schema():
    return {'type': 'function', 'function': {
        'name': FINISH_REPLY,
        'description': '完成本轮对话回复。每项是一条独立聊天消息，内容和分条位置由你决定。需要业务工具时先使用工具，获得结果后单独调用本工具。',
        'parameters': {'type': 'object', 'properties': {
            'messages': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['messages'], 'additionalProperties': False},
    }}


def reply_messages(message):
    value = message.metadata.get('replySequence')
    return tuple(value) if isinstance(value, (list, tuple)) and value and all(
        isinstance(item, str) and item.strip() for item in value) else ()


def message_pause(content):
    return min(1.8, max(.6, len(content) * .025))


async def wait_between_messages(content):
    await asyncio.sleep(message_pause(content))


@dataclass(eq=False)
class ReplyPlan:
    """An admitted plan; invalidation never cancels an in-flight operation."""
    current: bool = True
    run_id: str = ''
    task: object = None

    def invalidate(self):
        self.current = False


def private_reply(message):
    """Persist tool facts, but not any unshown natural-language suffix."""
    return replace(message, content='', metadata={
        **{key: value for key, value in message.metadata.items() if key != 'replySequence'},
        'naturalReply': True,
    })


class NaturalReplyProvider:
    """Keep prose private until one valid finish_reply; correct format once.

    Business tools remain in the ordinary loop. A finish call in a business
    batch is a premature draft and never executes or produces a visible reply.
    The wrapped ContextWindowProvider budgets every correction request.
    """
    def __init__(self, provider, *, authorized=lambda: True):
        self.provider, self.authorized = provider, authorized
        self.corrected = False

    def __getattr__(self, name):
        return getattr(self.provider, name)

    def _check(self, signal):
        if signal.is_cancelled() or not self.authorized():
            raise asyncio.CancelledError

    async def _response(self, kwargs):
        self._check(kwargs['signal'])
        done, text, calls = None, '', []
        async with aclosing(self.provider.stream_response(**kwargs)) as stream:
            async for event in stream:
                # A supplement invalidates at the end of this model step;
                # don't abandon a provider request or an already-running tool.
                if isinstance(event, TextDeltaEvent):
                    text += event.delta
                elif isinstance(event, ToolCallEndEvent):
                    calls.append(event.tool_call)
                elif isinstance(event, ProviderErrorEvent):
                    raise ValueError(event.error)
                elif isinstance(event, AssistantDoneEvent):
                    if done is not None:
                        raise ValueError('自然聊天模型返回了多个完成事件')
                    done = event.message
                elif isinstance(event, (ThinkingDeltaEvent, ProviderStartEvent)):
                    # Shiori buffers only reply prose. Public provider thinking
                    # must reach the UI while this model step is still running.
                    if kwargs['signal'].is_cancelled():
                        raise asyncio.CancelledError
                    # A newer user input invalidates this plan, but the in-flight
                    # model step must settle before its role ownership is released.
                    if self.authorized():
                        yield event
        self._check(kwargs['signal'])
        if done is None:
            raise ValueError('自然聊天模型未完整返回')
        yield AssistantDoneEvent(replace(done, content=done.content or text, tool_calls=done.tool_calls or tuple(calls)))

    @staticmethod
    def _validate(message):
        if message.stop_reason not in {'stop', 'tool_use'}:
            raise ValueError('回复被拒绝或截断，尚未形成完整消息列表')
        if len(message.tool_calls) != 1 or message.tool_calls[0].name != FINISH_REPLY:
            raise ValueError('请单独调用 finish_reply 完成本轮回复')
        call = message.tool_calls[0]
        if not call.arguments_complete or set(call.arguments) != {'messages'}:
            raise ValueError('finish_reply 只能包含有效的 messages 字段')
        values = call.arguments['messages']
        if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item.strip() for item in values):
            raise ValueError('messages 必须是非空字符串列表')
        return tuple(values)

    async def stream_response(self, **kwargs):
        schema = finish_reply_schema()
        if any(item.get('function', {}).get('name') == FINISH_REPLY for item in kwargs['tools']):
            raise ValueError('finish_reply 是保留的回复协议，不能注册为业务工具')
        request = {**kwargs, 'tools': (*kwargs['tools'], schema)}
        async with aclosing(self._response(request)) as stream:
            async for event in stream:
                if isinstance(event, AssistantDoneEvent):
                    message = event.message
                else:
                    yield event
        business = tuple(call for call in message.tool_calls if call.name != FINISH_REPLY)
        if business:
            # The loop still validates completion/JSON before executing any
            # tool. No draft prose or finish marker is stored as spoken text.
            metadata = dict(message.metadata)
            if isinstance(metadata.get('content_blocks'), list):
                # Keep signed thinking and business call signatures in provider
                # history, while removing prose and the premature completion.
                metadata['content_blocks'] = [block for block in metadata['content_blocks']
                    if isinstance(block, dict) and (block.get('type') == 'thinking'
                        or block.get('type') == 'tool_call' and block.get('name') != FINISH_REPLY)]
            yield AssistantDoneEvent(replace(message, content='', tool_calls=business, metadata=metadata))
            return
        try:
            sequence = self._validate(message)
        except ValueError as error:
            if self.corrected or message.stop_reason in {'error', 'aborted'} or message.metadata.get('refusal'):
                raise
            self.corrected = True
            correction = list(kwargs['messages'])
            correction.append(message)
            correction.extend(ToolResultMessage(call.id, call.name, f'回复未发送：{error}', is_error=True)
                              for call in message.tool_calls)
            correction.append(UserMessage(f'[回复格式纠正] {error}。刚才的回复尚未发送。'
                '请基于已有结果通过 finish_reply 提交 messages 列表，保留原本的意思。'
                '不要执行业务工具，不要解释发送格式。'))
            async with aclosing(self._response({**kwargs, 'messages': tuple(correction), 'tools': (schema,)})) as stream:
                async for event in stream:
                    if isinstance(event, AssistantDoneEvent):
                        message = event.message
                    else:
                        yield event
            sequence = self._validate(message)
        metadata = {key: value for key, value in message.metadata.items() if key != 'content_blocks'}
        yield AssistantDoneEvent(AssistantMessage('\n\n'.join(sequence), metadata={
            **metadata, 'replySequence': list(sequence), 'naturalReplyCorrected': self.corrected}))
