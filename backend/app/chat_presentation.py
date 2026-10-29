"""Read-only desktop presentation of the durable runtime transcript."""
import json
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .agent_runtime.progress import MAX_TEXT_BYTES, utf8_prefix
from .browser_use.safety import sanitize_browser_arguments, sanitize_browser_text


_PRIVATE_KEY = re.compile(r'token|secret|password|passwd|auth|cookie|signature|credential|api.?key|^key$|^sig$', re.I)
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)


def _safe_url(match):
    try:
        parts = urlsplit(match.group())
        authority = parts.netloc.rsplit('@', 1)[-1]
        query = [(key, '[redacted]' if _PRIVATE_KEY.search(key) or key.casefold() == 'code' else value)
                 for key, value in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, authority, parts.path, urlencode(query), ''))
    except ValueError:
        return '[invalid URL]'


def arguments_preview(name, arguments):
    """A bounded diagnostic preview, never the raw invocation payload."""
    if not isinstance(arguments, dict):
        return ''
    if name == 'mail.manage':
        return '邮件参数已隐藏'

    def scrub(value, depth=0):
        if depth > 6:
            return '…'
        if isinstance(value, dict):
            return {str(key)[:120]: '[redacted]' if _PRIVATE_KEY.search(str(key)) else scrub(item, depth + 1)
                    for key, item in list(value.items())[:30]}
        if isinstance(value, list):
            return [scrub(item, depth + 1) for item in value[:10]]
        if isinstance(value, str):
            return display_text(_URL.sub(_safe_url, value))[:500]
        return value

    safe = scrub(sanitize_browser_arguments(name or '', arguments))
    text = json.dumps(safe, ensure_ascii=False, separators=(',', ':'))
    return '无参数' if text == '{}' else text[:117] + '...' if len(text) > 120 else text


def display_text(value):
    return utf8_prefix(sanitize_browser_text(value)[0], MAX_TEXT_BYTES) if isinstance(value, str) else ''


def thinking_text(metadata):
    # Never publish opaque signatures or redacted provider blocks. Older
    # transcripts have only `thinking`; new transcripts retain the block flag.
    blocks = metadata.get('content_blocks')
    if not isinstance(blocks, list):
        blocks = metadata.get('thinking', [])
    return display_text('\n\n'.join(block.get('text', '') for block in blocks
        if isinstance(block, dict) and block.get('type', 'thinking') == 'thinking'
        and not block.get('redacted') and isinstance(block.get('text'), str))) if isinstance(blocks, list) else ''


def run_process(messages, run_id):
    if not run_id:
        return []
    rows = [row for row in messages if row.runId == run_id]
    results = {row.toolCallId: row for row in rows if row.messageType == 'tool_result'}
    process, groups = [], set()
    for row in rows:
        if row.role != 'assistant' or row.messageType not in {'text', 'tool_call'}:
            continue
        if row.metadata.get('replySequenceIndex', 0) != 0:
            continue
        group = row.metadata.get('toolCallGroupId', row.id)
        if group not in groups:
            groups.add(group)
            thinking = thinking_text(row.metadata) or display_text(row.metadata.get('streamThinking'))
            if thinking:
                process.append({'type': 'thinking', 'id': f'{group}:thinking', 'text': thinking})
            if row.messageType == 'tool_call' and row.content:
                process.append({'type': 'text', 'id': f'{group}:text', 'text': display_text(row.content)})
        if row.messageType == 'tool_call':
            result = results.get(row.toolCallId)
            details = result.toolResult if result and isinstance(result.toolResult, dict) else {}
            status = details.get('status', 'failed' if result and result.isError else 'succeeded' if result else 'unknown')
            if status not in {'succeeded', 'failed', 'unknown', 'denied', 'invalid_arguments', 'timed_out', 'cancelled'}:
                status = 'unknown'
            process.append({'type': 'tool', 'id': row.toolCallId, 'name': row.toolName,
                'argumentsPreview': arguments_preview(row.toolName, row.toolArguments),
                'status': status, 'result': display_text(result.content) if result else '尚无最终结果'})
    return process


def run_metrics(messages, run_id):
    """Sum actual provider usage once per model step, not once per tool or split reply."""
    total, groups, complete = 0, set(), True
    metrics = {}
    for row in messages:
        if row.runId != run_id or row.role != 'assistant' or row.messageType not in {'text', 'tool_call'}:
            continue
        if row.metadata.get('replySequenceIndex', 0) != 0:
            continue
        group = row.metadata.get('toolCallGroupId', row.id)
        if group in groups:
            continue
        groups.add(group)
        # These wrappers can make extra model requests without retaining a full
        # usage record for each one. Never label the last request as the total.
        if row.metadata.get('emptyReplyRecovery') or row.metadata.get('naturalReplyCorrected') or row.metadata.get('auxiliaryUsageIncomplete'):
            complete = False
        duration = row.metadata.get('thinkingDurationMs')
        if type(duration) in {int, float} and math.isfinite(duration) and duration >= 0:
            metrics['thinkingDurationMs'] = duration
        usage = row.metadata.get('usage')
        if not isinstance(usage, dict):
            complete = False
            continue
        keys = ['input_tokens', 'output_tokens']
        if row.metadata.get('api') in {'anthropic-messages', 'bedrock-converse'}:
            keys += ['cache_read_tokens', 'cache_write_tokens']
        counts = [usage.get(key, 0) for key in keys]
        if any(type(value) not in {int, float} or value < 0 or not math.isfinite(value) for value in counts) or not any(counts):
            complete = False
        else:
            total += sum(counts)
    if complete and groups:
        metrics['totalTokens'] = total
    return metrics


def process_target(messages, run_id):
    return next((row.id for row in messages if row.runId == run_id and row.role == 'assistant'
        and row.messageType == 'text'), None)
