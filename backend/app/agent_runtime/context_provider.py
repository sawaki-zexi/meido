"""Budget ordinary requests and compact complete, durably sourced turns.

Retention selection adapted from Shiori be6343d session/turns.py and
core/compaction.py. Copyright (c) 2026 YinFengWindy.
MIT: third_party/shiori-agent/LICENSE.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import json
import time
from contextlib import aclosing, nullcontext
from dataclasses import asdict, replace

from ..ai.model_types import (ModelStreamDone, ModelStreamError, ModelSystemMessage, ModelTextBlock, ModelTextDelta,
                             ModelUserMessage, ModelThinkingBlock, ModelThinkingDelta, ModelThinkingStart)
from ..ai.output_usage import parse_output_usage
from .types import UserMessage, AssistantMessage, ToolResultMessage, AttachmentContent
from ..ai.model_adapter import supports_auxiliary_thinking_control
from .usage_anchor import estimate_tokens
from .summary_batches import next_summary_batch, summary_source
from .summary_validation import SUMMARY_FIELDS, SUMMARY_PROMPT, measured_summary, shortening_payload

logger = logging.getLogger(__name__)
# Match Shiori's default provider request allowance for auxiliary summaries.
# This remains independent of the subsequent ordinary-response deadline.
SUMMARY_TIMEOUT_SECONDS = 90.0
MINIMAL_PROMPT = (
    "本次上下文过长且工作摘要未能完成，较早原文暂未放入本次请求，原始记录仍保留。"
    "本次依据当前用户输入、已有合法摘要、附件和已执行工具结果继续处理。"
    "工具能力以本次提供的列表为准；不要重做已执行操作、声称取得没有的结果或编造缺失历史。"
    "如证据不足请明确说明；需要其他能力时，可通过本次提供的 tool_search 发现并加载。"
)

def budget(configuration, settings, output):
    capacity = configuration.contextWindowTokens
    available = capacity - output - settings.contextSafetyMarginTokens
    if available <= 0:
        raise ValueError('模型容量扣除回复预留与安全余量后没有输入空间，请调整设置')
    trigger = min(available, configuration.modelAutoCompactTokenLimit or max(1, int(capacity * settings.contextTriggerRatio)))
    target = min(available, max(1, int(capacity * settings.contextTargetRatio)), trigger)
    if target >= trigger:
        raise ValueError('当前容量与预留没有留下独立的压缩目标，请调整设置')
    return available, trigger, target


class ContextWindowProvider:
    # The auxiliary request has its own deadline; only the final model stream
    # consumes the ordinary provider timeout supplied by the loop.
    manages_request_timeout = True
    supports_protocol_tools = True
    def __init__(self, provider, settings, windows, *, role_id, session_key, scope, authority, authorized, ensure_coverage, on_diagnostic=None):
        self.provider, self.settings, self.windows = provider, settings, windows
        self.role_id, self.session_key, self.scope, self.authority = role_id, session_key, scope, authority
        self.authorized, self.ensure_coverage = authorized, ensure_coverage
        bind_usage = getattr(provider, 'bind_usage_context', None)
        if callable(bind_usage):
            bind_usage((role_id, session_key, scope, authority))
        self.requests = 0
        self.compactions = 0
        self.last_input_estimate = 0
        self.last_input_source = 'local'
        self.on_diagnostic = on_diagnostic
        self.on_phase = None
        self.started = time.monotonic()
        self.tool_count = 0
        self.compaction_failed = False
        self.degraded = False
        self.compaction_diagnostic = {}
        self.summary_attempts = []
        self.current_input = None
        self.capabilities = None
        self.tool_preparation = None
        self.request_context = None
        self.prepared_request = None

    def bind_capabilities(self, capabilities):
        self.capabilities = capabilities

    async def inspect(self, messages, signal, *, protocol_tools=()):
        """Measure a saved window; never publish tools, reserve or compact it."""
        state = await self._io(signal, self.windows.load, self.role_id, self.session_key, self.scope, self.authority)
        await self._prepare_inputs(signal)
        removed = set(state['removed'])
        raw = [message for message in messages if not (set(message.source_message_ids) & removed)]
        tools = self._window_tools(raw, (), protocol_tools=protocol_tools)
        visible = ([self._summary_message(state['summary'])] if state['summary'] else []) + raw
        tokens = self._estimate('', visible, tools)
        configuration = self.provider.configuration
        output = self.provider.max_response_tokens or self.settings.maxTokens
        available = trigger = target = None
        reason = ''
        if configuration.contextWindowTokens is None:
            reason = '模型尚未配置上下文容量'
        else:
            available, trigger, target = budget(configuration, self.settings, output)
        return {'inputTokens': tokens, 'source': self.last_input_source,
            'configurationId': configuration.id, 'model': configuration.model,
            'capacity': configuration.contextWindowTokens, 'inputLimit': available,
            'outputReserve': output, 'safetyMargin': self.settings.contextSafetyMarginTokens,
            'compactTrigger': trigger, 'compactTarget': target, 'reason': reason}, state

    async def _io(self, signal, operation, *args, drain_on_cancel=False, **kwargs):
        worker = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        try:
            result = await asyncio.shield(worker) if drain_on_cancel else await worker
        except asyncio.CancelledError:
            signal.cancel()
            # A window write belongs to this owner until its transaction exits.
            # Read-only workers can be abandoned; they only return input values.
            if drain_on_cancel:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        signal.cancel()
                    except Exception:
                        break
                if worker.done() and not worker.cancelled():
                    worker.exception()
            raise
        signal.raise_if_cancelled()
        return result

    async def _prepare_inputs(self, signal):
        signal.raise_if_cancelled()
        prepare = getattr(self.provider, 'prepare_context', None)
        if callable(prepare):
            # Schema and namespace use the same metadata; no second host read.
            self.request_context = await prepare(signal, namespace='' if self.capabilities is not None else None)
        if self.capabilities is not None:
            # This final await rechecks registry edits during context I/O too.
            self.tool_preparation = await self.capabilities.prepare_metadata(signal)

    def _window_view(self, raw, *, restore=True):
        if not restore or self.capabilities is None:
            return None
        current = self.capabilities.current()
        return (current._select((item.name for item in current.snapshot.tools), preparation=self.tool_preparation) if current._discovery is None
                else current.window_view(raw, preparation=self.tool_preparation))

    def _publish_window(self, view):
        if view is not None:
            view.publish_window()
            self.capabilities = view

    def _window_tools(self, raw, fallback, *, commit=False, protocol_tools=(), restore=True):
        view = self._window_view(raw, restore=restore)
        if view is None:
            return fallback
        if commit:
            self._publish_window(view)
        return (*view.provider_schemas(), *protocol_tools)

    async def _reserve(self, signal, *, auxiliary=False):
        signal.raise_if_cancelled()
        if not await self._io(signal, self.authorized):
            raise PermissionError('对话来源许可已变化，请重新开始回复')
        if self.requests + int(auxiliary) >= self.settings.maxIterations:
            raise ValueError('本次运行的模型请求步数已用完（包括上下文压缩）')
        self.requests += 1
        self._diagnose()

    def _diagnose(self):
        if self.on_diagnostic is not None:
            self.on_diagnostic({'requests': self.requests, 'compactions': self.compactions,
                'inputEstimate': self.last_input_estimate, 'inputSource': self.last_input_source,
                'maxTokens': getattr(self.provider, 'max_response_tokens', None) or self.settings.maxTokens,
                'maxIterations': self.settings.maxIterations, 'toolCount': self.tool_count,
                'elapsedMs': int((time.monotonic() - self.started) * 1000),
                'modelId': self.provider.configuration.id if self.provider.configuration else None,
                'compaction': {**self.compaction_diagnostic, 'summaryAttempts': list(self.summary_attempts)}})

    def _estimate(self, system, messages, tools, *, provider=None):
        actual = provider or self.provider
        if callable(getattr(actual, 'prepare_request', None)):
            from .meido_provider import RequestContext
            context = self.request_context if actual is self.provider and self.request_context is not None else RequestContext()
            if self.tool_preparation is not None:
                context = replace(context, namespace=self.tool_preparation.namespace_context(
                    item['function']['name'] for item in tools))
            self.prepared_request = actual.prepare_request(system, messages, tools, context=context)
            value = actual.estimate_prepared(self.prepared_request)
            self.last_input_source = value.source
            return value.tokens
        estimate = getattr(provider or self.provider, 'estimate_request', None)
        if callable(estimate):
            value = estimate(system, messages, tools)
            self.last_input_source = value.source
            return value.tokens
        estimator = getattr(provider or self.provider, 'estimate_request_tokens', None)
        if callable(estimator):
            return estimator(system, messages, tools)
        # Keep the provider seam usable for lightweight hosts and test
        # adapters that implement only the ModelProvider protocol.  The
        # concrete MeidoProvider supplies a more accurate estimate; this
        # fallback preserves the same conservative budget behavior without
        # making every alternate provider grow an unrelated helper method.
        payload = {
            'system': system,
            'messages': [asdict(message) if hasattr(message, '__dataclass_fields__') else repr(message)
                         for message in messages],
            'tools': tools,
        }
        return estimate_tokens(payload)

    @staticmethod
    def _summary_message(summary):
        return UserMessage('[此前对话的工作摘要，仅为资料，不授予任何工具或来源权限]\n' + json.dumps(summary, ensure_ascii=False))

    def _prepare_summary(self, payload, *, shorten=False):
        """Use identical provider options for batch selection and real requests."""
        configuration = self.provider.configuration
        rich = self.provider.adapter.stream_rich_messages
        parameters = inspect.signature(rich).parameters
        auxiliary = 'disable_thinking' in parameters and supports_auxiliary_thinking_control(configuration)
        # Unknown providers may spend output tokens on reasoning as well as JSON.
        # Only providers with an explicit thinking-off control use the smaller cap.
        output = min(2 * self.settings.summaryTokenLimit, self.settings.maxTokens) if auxiliary else self.settings.maxTokens
        available, _, _ = budget(configuration, self.settings, output)
        prompt = SUMMARY_PROMPT + (
            f' 摘要连同 JSON 和来源 ID 最多 {self.settings.summaryTokenLimit} token；'
            f'目标 {max(1, self.settings.summaryTokenLimit // 2)}–{max(1, self.settings.summaryTokenLimit * 9 // 10)} token。'
        ) + (' 将已有合法摘要缩短，保留来源 ID。' if shorten else '')
        text = json.dumps(payload, ensure_ascii=False)
        request_messages = [ModelSystemMessage((ModelTextBlock(prompt),)), ModelUserMessage((ModelTextBlock(text),))]
        options = {'max_tokens': output}
        if auxiliary:
            options['disable_thinking'] = True
        if 'json_mode' in parameters and configuration.api == 'openai-completions':
            options['json_mode'] = True
        # Summary calls deliberately opt out of ordinary usage anchors, both
        # when checking the budget and when consuming the response.
        prepare = getattr(self.provider.adapter, 'prepare_input', None)
        if callable(prepare):
            from .usage_anchor import input_cost
            estimate = input_cost(prepare(request_messages, configuration, [], **options))
        else:
            estimate = estimate_tokens({'messages': [asdict(message) for message in request_messages], 'tools': []}) + 32
        return request_messages, options, estimate, available

    def _summary_fits(self, payload):
        _, _, estimate, available = self._prepare_summary(payload)
        return estimate <= available

    async def _summarize(self, payload, signal, *, shorten=False, batch=1):
        request_messages, options, estimate, available = self._prepare_summary(payload, shorten=shorten)
        configuration = self.provider.configuration
        rich = self.provider.adapter.stream_rich_messages
        parameters = inspect.signature(rich).parameters
        def check_request():
            signal.raise_if_cancelled()
            guard = getattr(self.provider, 'request_guard', None)
            if guard is not None:
                guard()
            elif not self.authorized():
                raise PermissionError('对话来源许可已变化，请重新开始回复')
        if 'cancel_check' in parameters:
            options['cancel_check'] = check_request
        result = ''
        done = False
        has_thinking = False
        max_characters = max(4096, options['max_tokens'] * 32)
        attempt = {'maxTokens': options['max_tokens'], 'shortening': shorten,
                   'thinkingDisabled': options.get('disable_thinking', False), 'batch': batch,
                   'inputEstimate': estimate, 'inputSource': 'local',
                   'stopReason': None, 'outcome': 'started'}
        self.summary_attempts.append(attempt)
        started = time.monotonic()
        try:
            if estimate > available:
                attempt['outcome'] = 'input_overflow'
                raise ValueError('待压缩前缀超出摘要请求容量，请使用更大上下文的模型')
            await self._reserve(signal, auxiliary=True)
            # Auxiliary work cannot consume the entire ordinary-response deadline.
            async with asyncio.timeout(SUMMARY_TIMEOUT_SECONDS):
                async with aclosing(rich(request_messages, configuration, [], **options)) as stream:
                    async for event in stream:
                        signal.raise_if_cancelled()
                        if 'cancel_check' not in parameters:
                            await self._io(signal, check_request)
                        if isinstance(event, ModelTextDelta):
                            result += event.text
                            if len(result) > max_characters:
                                attempt['outcome'] = 'response_too_large'
                                raise ValueError('摘要响应超过本地体积上限')
                        elif isinstance(event, (ModelThinkingStart, ModelThinkingDelta)):
                            has_thinking = True
                        elif isinstance(event, ModelStreamError):
                            attempt['outcome'] = 'stream_error'
                            raise ValueError('生成工作摘要失败')
                        elif isinstance(event, ModelStreamDone):
                            metadata = event.message.metadata
                            attempt['stopReason'] = metadata.stop_reason
                            has_thinking |= any(isinstance(block, ModelThinkingBlock) for block in event.message.content)
                            usage = metadata.usage.output_usage or parse_output_usage(
                                metadata.usage.output_tokens or None, metadata.usage.reasoning_tokens or None)
                            usage = usage.for_response(has_thinking=has_thinking)
                            attempt['usage'] = {key: value for key, value in {
                                'input_tokens': metadata.usage.input_tokens, 'output_tokens': usage.completion_tokens,
                                'reasoning_tokens': usage.reasoning_tokens}.items() if value is not None}
                            attempt.update(providerOutputTokens=usage.visible_tokens, fallbackReason=usage.unavailable_reason)
                            if metadata.stop_reason not in {None, 'stop', 'end_turn', 'completed'}:
                                attempt['outcome'] = 'truncated' if metadata.stop_reason in {'length', 'max_tokens'} else 'incomplete'
                                raise ValueError('工作摘要被截断或未正常完成')
                            text_blocks = [block.text for block in event.message.content if isinstance(block, ModelTextBlock)]
                            if sum(map(len, text_blocks)) > max_characters:
                                attempt['outcome'] = 'response_too_large'
                                raise ValueError('摘要响应超过本地体积上限')
                            result = ''.join(text_blocks) or result
                            done = True
                        if 'cancel_check' in parameters:
                            # Buffered SSE still needs to yield to HTTP/Stop tasks.
                            await asyncio.sleep(0)
            if not done:
                attempt['outcome'] = 'incomplete'
                raise ValueError('工作摘要未完整返回')
            if not await self._io(signal, self.authorized):
                raise PermissionError('对话来源许可已变化，请重新开始回复')
            attempt['outcome'] = 'returned'
            return result
        except BaseException as error:
            if attempt['outcome'] == 'started':
                attempt['outcome'] = ('timeout' if isinstance(error, TimeoutError) else
                    'cancelled' if isinstance(error, asyncio.CancelledError) else
                    'source_revoked' if isinstance(error, PermissionError) else 'failed')
            attempt['errorType'] = type(error).__name__
            raise
        finally:
            attempt['elapsedMs'] = int((time.monotonic() - started) * 1000)
            self._diagnose()

    async def _compact(self, system, raw, tools, state, signal, available, target, *, protocol_tools=(), restore=True):
        self.compaction_diagnostic['step'] = 'select_prefix'
        # Shiori session/turns.py: consecutive inputs and pending calls form one
        # lifecycle; only an assistant completion closes it. The current input
        # and all its exchanges remain outside the compactable prefix.
        completed, start, complete, pending = [], None, False, set()
        for index, message in enumerate(raw):
            if message is self.current_input or (self.current_input is not None and self.current_input.source_message_ids
                    and message.source_message_ids == self.current_input.source_message_ids):
                if start is not None and complete:
                    completed.append((start, index))
                break
            if isinstance(message, UserMessage) and (start is None or complete):
                if start is not None:
                    completed.append((start, index))
                start, complete = index, False
            if isinstance(message, AssistantMessage):
                pending.update(call.id for call in message.tool_calls)
                complete = not message.tool_calls and not pending and message.stop_reason not in {'aborted', 'error'}
            elif isinstance(message, ToolResultMessage):
                pending.discard(message.tool_call_id)
        choices = list(dict.fromkeys(completed[-keep][0] if keep else completed[-1][1]
            for keep in range(min(self.settings.compactionRetainedTurns, len(completed)), -1, -1))) if completed else []
        selected = None
        hard_fit = None
        for cut in choices:
            signal.raise_if_cancelled()
            prefix = raw[:cut]
            if not prefix:
                continue
            if any(not message.source_message_ids for message in prefix):
                break  # Never evict an unfinished, in-memory tool chain.
            selected = cut
            projected = self._estimate(system, raw[cut:], self._window_tools(raw[cut:], tools, protocol_tools=protocol_tools, restore=restore)) + self.settings.summaryTokenLimit
            if hard_fit is None and projected < available:
                hard_fit = cut
            if projected <= target:
                break
        else:
            selected = hard_fit if hard_fit is not None else selected
        if selected is None or self._estimate(system, raw[selected:], self._window_tools(raw[selected:], tools, protocol_tools=protocol_tools, restore=restore)) >= available:
            raise ValueError('完整保留轮或当前工具链超出输入预算，无法安全压缩')
        prefix, retained = raw[:selected], raw[selected:]
        ids = list(dict.fromkeys([*state['removed'], *(identifier for message in prefix for identifier in message.source_message_ids)]))
        prepared = await self._io(signal, self.windows.prepare, self.role_id, self.session_key, self.scope, self.authority, ids,
                                  drain_on_cancel=True)
        if prepared['version'] != state['version']:
            raise ValueError('工作窗口已变化，请重新开始回复')
        records = [summary_source(message) for message in prefix]
        allowed = set(identifier for message in prefix for identifier in message.source_message_ids)
        if state['summary']:
            allowed.update(state['summary']['source_message_ids'])
        summary = state['summary']
        batch = 0
        while records:
            signal.raise_if_cancelled()
            batch += 1
            self.compaction_diagnostic.update(step='select_batch', batch=batch)
            count, payload = next_summary_batch(summary, records, self._summary_fits)
            self.compaction_diagnostic['step'] = 'request_summary'
            response = await self._summarize(payload, signal, batch=batch)
            self.compaction_diagnostic['step'] = 'validate_summary'
            summary = measured_summary(response, allowed, self.settings.summaryTokenLimit, self.summary_attempts[-1])
            if self.summary_attempts[-1]['outcome'] == 'oversized':
                self.compaction_diagnostic['step'] = 'shorten_summary'
                feedback = shortening_payload(summary, self.summary_attempts[-1], self.settings.summaryTokenLimit)
                response = await self._summarize(feedback, signal, shorten=True, batch=batch)
                self.compaction_diagnostic['step'] = 'validate_shortened_summary'
                summary = measured_summary(response, allowed, self.settings.summaryTokenLimit, self.summary_attempts[-1])
            if self.summary_attempts[-1]['outcome'] == 'oversized':
                raise ValueError('工作摘要超过长度上限')
            records = records[count:]
        visible = [self._summary_message(summary), *retained]
        await self._prepare_inputs(signal)
        estimated = self._estimate(system, visible, self._window_tools(retained, tools, protocol_tools=protocol_tools, restore=restore))
        if estimated >= available or estimated >= self.last_input_estimate:
            raise ValueError('压缩后仍超出输入容量或未减少输入，原文已保留')
        self.compaction_diagnostic['step'] = 'commit_summary'
        covered = await self.ensure_coverage(ids)
        signal.raise_if_cancelled()
        # Coverage may await memory work while the current tool registry changes.
        # Capture the complete candidate and its issued visibility before the
        # durable cut. Commit may await I/O, but success sends this exact input.
        await self._prepare_inputs(signal)
        view = self._window_view(retained, restore=restore)
        request_tools = (*view.provider_schemas(), *protocol_tools) if view is not None else tools
        estimated = self._estimate(system, visible, request_tools)
        request = self.prepared_request
        if request is not None and request.configuration is not None and request.configuration.contextWindowTokens is not None:
            available, _, _ = budget(request.configuration, self.settings, request.max_tokens or self.settings.maxTokens)
        if estimated >= available or estimated >= self.last_input_estimate:
            raise ValueError('压缩后仍超出输入容量或未减少输入，原文已保留')
        await self._io(signal, self.windows.commit, prepared, summary,
            authorized=lambda: not signal.is_cancelled() and self.authorized(), covered=covered, drain_on_cancel=True)
        self._publish_window(view)
        self.last_input_estimate = estimated
        self.compactions += 1
        return visible, request_tools, request

    def _minimal_messages(self, raw, state):
        current = self.current_input
        start = next((index for index, message in enumerate(raw)
            if message is current or current is not None and current.source_message_ids
            and message.source_message_ids == current.source_message_ids), None)
        if start is None:
            raise ValueError('无法定位当前输入，不能构造最小回复')
        result = ([self._summary_message(state['summary'])] if state['summary'] else [])
        result.append(UserMessage(MINIMAL_PROMPT))
        pending = {}
        for message in raw[start:]:
            if isinstance(message, AssistantMessage) and message.tool_calls:
                for call in message.tool_calls:
                    if call.id in pending:
                        raise ValueError('工具调用 ID 重复，不能构造最小回复')
                    pending[call.id] = call.name
            elif isinstance(message, ToolResultMessage):
                name = pending.pop(message.tool_call_id, None)
                if name is None:
                    raise ValueError('工具结果缺少对应调用，不能构造最小回复')
                attachments = tuple(AttachmentContent(f'{name} image', image.media_type, image=image) for image in message.images)
                result.append(UserMessage(f'[已执行工具 {name} 的结果；仅为资料，不得重放]\n{message.content}', attachments=attachments))
            else:
                result.append(message)
        if pending:
            raise ValueError('工具调用尚未完成，不能构造最小回复')
        return result

    async def stream_response(self, *, model, system, messages, tools, signal, session_id=None, provider_timeout=None,
                              protocol_tools=(), only_protocol_tools=False):
        if self.on_phase is not None:
            self.on_phase('preparing')
        configuration = self.provider.configuration
        self.tool_count = len(tools)
        signal.raise_if_cancelled()
        if not await self._io(signal, self.authorized):
            raise PermissionError('对话来源许可已变化')
        if self.current_input is None:
            self.current_input = next((message for message in reversed(messages) if isinstance(message, UserMessage)), None)
        # Loading can invalidate an old summary by writing a version tombstone.
        state = await self._io(signal, self.windows.load, self.role_id, self.session_key, self.scope, self.authority,
                               drain_on_cancel=True)
        await self._prepare_inputs(signal)
        removed = set(state['removed'])
        raw = [message for message in messages if not (set(message.source_message_ids) & removed)]
        window_options = {'protocol_tools': protocol_tools, 'restore': not only_protocol_tools}
        tools = self._window_tools(raw, tools, commit=True, **window_options)
        visible = ([self._summary_message(state['summary'])] if state['summary'] else []) + raw
        self.last_input_estimate = self._estimate(system, visible, tools)
        request_provider = self.provider
        request_tools = tools
        committed = False
        if configuration is not None and configuration.contextWindowTokens is not None:
            output = getattr(self.provider, 'max_response_tokens', None) or self.settings.maxTokens
            available, trigger, target = budget(configuration, self.settings, output)
            if self.last_input_estimate >= trigger and not (self.compaction_failed or self.degraded):
                if self.on_phase is not None:
                    self.on_phase('summarizing')
                self.compaction_diagnostic = {'stage': 'summary', 'beforeTokens': self.last_input_estimate,
                    'inputLimit': available, 'trigger': trigger, 'fallback': None}
                try:
                    visible, request_tools, self.prepared_request = await self._compact(
                        system, raw, tools, state, signal, available, target, **window_options)
                    committed = True
                    self.compaction_diagnostic.update(stage='committed', afterTokens=self.last_input_estimate)
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                    # A failed optional summary is never a new historical fact.
                    # Recheck source authority before either degradation path.
                    signal.raise_if_cancelled()
                    if not await self._io(signal, self.authorized):
                        raise
                    self.compaction_failed = True
                    self.compaction_diagnostic.update(stage='failed', errorType=type(error).__name__)
                    logger.warning('Context compaction failed role=%s run-scope=%s type=%s',
                                   self.role_id, self.scope, type(error).__name__)
                    # Failure follows model/coverage/commit waits. Reprepare
                    # against the unchanged raw window before choosing fallback.
                    await self._prepare_inputs(signal)
                    request_tools = self._window_tools(raw, tools, **window_options)
                    self.last_input_estimate = self._estimate(system, visible, request_tools)
                    configuration = self.provider.configuration
                    if configuration is not None and configuration.contextWindowTokens is not None:
                        output = getattr(self.provider, 'max_response_tokens', None) or self.settings.maxTokens
                        available, _, _ = budget(configuration, self.settings, output)
                    if self.last_input_estimate < available:
                        self.compaction_diagnostic['fallback'] = 'original'
                finally:
                    self._diagnose()
            if not committed and (self.degraded or self.last_input_estimate >= available):
                # Hard overflow has one transient minimal rendering; never cut SQLite
                # history or publish the failed summary. Preserve current receipts.
                request_provider = self.provider.minimal_provider()
                visible = self._minimal_messages(raw, state)
                # Shorten context, not authorization: discovery and actual work
                # must still be possible when the supplied schemas fit. If they
                # do not fit, fail explicitly instead of silently claiming no tools.
                request_tools = self._window_tools(raw, tools, **window_options)
                self.last_input_estimate = self._estimate(system, visible, request_tools, provider=request_provider)
                self.degraded = True
                self.compaction_diagnostic.update(fallback='minimal', toolsDisabled=False,
                                                  afterTokens=self.last_input_estimate)
                self._diagnose()
                if self.last_input_estimate >= available:
                    raise ValueError('当前输入、必要约束或已执行工具结果仍超出输入容量；原历史已保留')
        if not committed:
            if self.compaction_diagnostic:
                request_tools = self._window_tools(raw, request_tools, commit=True, **window_options)
            self.last_input_estimate = self._estimate(system, visible, request_tools, provider=request_provider)
            if configuration is not None and configuration.contextWindowTokens is not None and self.last_input_estimate >= available:
                raise ValueError('当前输入与工具声明仍超出输入容量；原历史已保留')
        self.tool_count = len(request_tools)
        await self._reserve(signal)
        if self.on_phase is not None:
            self.on_phase('model')
        timeout = asyncio.timeout(provider_timeout) if provider_timeout is not None else nullcontext()
        source = (request_provider.stream_prepared(self.prepared_request, system=system, messages=visible, signal=signal)
                  if callable(getattr(request_provider, 'stream_prepared', None)) else
                  request_provider.stream_response(model=model, system=system, messages=visible, tools=request_tools,
                                                   signal=signal, session_id=session_id))
        async with timeout, aclosing(source) as stream:
            async for event in stream:
                yield event
