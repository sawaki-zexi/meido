"""Read-only desktop presentation of the durable runtime transcript."""
from .agent_runtime.progress import MAX_TEXT_BYTES, utf8_prefix
from .browser_use.safety import sanitize_browser_text


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
                'status': status, 'result': display_text(result.content) if result else '尚无最终结果'})
    return process


def process_target(messages, run_id):
    return next((row.id for row in messages if row.runId == run_id and row.role == 'assistant'
        and row.messageType == 'text'), None)
