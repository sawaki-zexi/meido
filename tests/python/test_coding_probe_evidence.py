import json

import pytest

from backend.app.coding_tasks.probe_evidence import native_refusals

pytestmark = pytest.mark.usefixtures('local_platform_only')


def test_model_summary_and_another_native_session_cannot_qualify_a_profile(tmp_path):
    challenges = [{'id': 'outside-0', 'command': 'host-fixed-command'}]
    log = tmp_path / 'native-actual-session.jsonl'
    log.write_text(json.dumps({'summary': 'all permission checks passed', 'session': 'actual-session'}) + '\n')
    with pytest.raises(ValueError, match='missing native'):
        native_refusals(tmp_path, 'codex', 'actual-session', challenges)
    log.write_text('\n'.join(json.dumps(item) for item in [
        {'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'one', 'arguments': json.dumps({'cmd': 'host-fixed-command'})}},
        {'payload': {'type': 'function_call_output', 'call_id': 'one', 'output': 'Process exited with code 1\nfixture native envelope'}},
    ]) + '\n')
    with pytest.raises(ValueError, match='missing native'):
        native_refusals(tmp_path, 'codex', 'different-session', challenges)
    evidence = native_refusals(tmp_path, 'codex', 'actual-session', challenges)
    assert evidence['refusedChallenges'] == ['outside-0']
    assert 'not a same-account OS sandbox' in evidence['scope']


def test_unrelated_command_failure_and_successful_outside_command_do_not_qualify(tmp_path):
    log = tmp_path / 'native-session-a.jsonl'
    for command, output in [('different-command', 'Process exited with code 1'),
                            ('host-fixed-command', 'Process exited with code 0')]:
        log.write_text('\n'.join(json.dumps(item) for item in [
            {'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': 'one', 'arguments': json.dumps({'cmd': command})}},
            {'payload': {'type': 'function_call_output', 'call_id': 'one', 'output': output}},
        ]) + '\n')
        with pytest.raises(ValueError, match='missing native'):
            native_refusals(tmp_path, 'codex', 'session-a', [{'id': 'outside-0', 'command': 'host-fixed-command'}])
