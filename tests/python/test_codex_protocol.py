import json

import pytest

from backend.app.coding_tasks.codex_protocol import CodexProtocol

SESSION = '07e35c16-cd89-40c9-aa4f-ac9257193252'


def frames(*events):
    return b''.join((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8') for event in events)


def test_split_utf8_jsonl_proves_one_exact_session_and_terminal_turn():
    parser = CodexProtocol(expected_session=SESSION)
    data = frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'id': '0', 'type': 'agent_message', 'text': '完整中文🙂'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 3}})
    for offset in range(0, len(data), 3):
        parser.feed(data[offset:offset+3])
    assert parser.finish(returncode=0) == SESSION


@pytest.mark.parametrize('data,exit_code', [
    (frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'turn.started'}), 0),
    (frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'turn.failed', 'error': {'message': 'no'}}), 0),
    (frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'error', 'message': 'no'}), 0),
    (frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'turn.started'}, {'type': 'turn.completed'}), 1),
    (frames({'type': 'thread.started', 'thread_id': 'bfe59f15-2903-4b7b-9de6-f70fe91dbd29'}), 0),
    (frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'thread.started', 'thread_id': SESSION}), 0),
    (b'{"type":"thread.started","type":"turn.completed"}\n', 0),
    (b'\xff\n', 0), (b'x' * (65536 + 1), 0),
    (frames({'type': 'thread.started', 'thread_id': SESSION}, {'type': 'turn.started'}, {'type': 'turn.completed'}) + b'{', 0),
], ids=['no-terminal', 'failed', 'error', 'nonzero-exit', 'wrong-session', 'second-session',
        'duplicate-key', 'bad-utf8', 'frame-budget', 'truncated'])
def test_incomplete_wrong_or_over_budget_native_protocol_never_proves_success(data, exit_code):
    parser = CodexProtocol(expected_session=SESSION)
    with pytest.raises(ValueError):
        parser.feed(data)
        parser.finish(returncode=exit_code)
