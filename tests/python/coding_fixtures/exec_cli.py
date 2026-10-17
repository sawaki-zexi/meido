"""Synthetic local Codex exec protocol peer. No credentials, model or network."""
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
if args == ['--version']:
    print('codex-cli 0.159.3')
    raise SystemExit(0)
assert args[0] == 'exec' and args[-1] == '-'
assert '--json' in args and '--ignore-user-config' in args and '--ignore-rules' in args
assert 'approval_policy="never"' in args and 'sandbox_workspace_write.network_access=false' in args
assert 'sandbox_mode="workspace-write"' in args
assert not any(arg in args for arg in ['--last', '--all', '--ephemeral', '--worktree', '--yolo'])
home = Path(os.environ['CODEX_HOME'])
assert home.is_relative_to(Path(os.environ['HOME']))
handoff = json.loads(sys.stdin.buffer.read().decode('utf-8'))
if handoff['requirement'] == 'hold until cancellation':
    time.sleep(60)
session_path = home / 'fixture-session.json'
if len(args) > 1 and args[1] == 'resume':
    session = args[2]
    assert session == json.loads(session_path.read_text())['id']
    assert '--sandbox' not in args and '--cd' not in args
else:
    assert args[args.index('--sandbox') + 1] == 'workspace-write'
    session = '07e35c16-cd89-40c9-aa4f-ac9257193252'
    session_path.write_text(json.dumps({'id': session}))
result = {'task_id': handoff['taskId'], 'run_id': handoff['attemptId'], 'status': 'completed',
          'summary': '真实本地 fixture 中文🙂', 'validation': handoff['validationIds'], 'blockers': []}
requirement = handoff['requirement']
mode = requirement.removeprefix('fault:') if requirement.startswith('fault:') else ''
if requirement == 'needs-owner-input':
    result['status'] = 'needs_input'
    result['blockers'] = ['owner clarification required']
if mode == 'stale-result':
    result['run_id'] = 'attempt_previous'
Path('fixture.txt').write_text(requirement, encoding='utf-8')
Path('observed.json').write_text(json.dumps({'args': args, 'cwd': str(Path.cwd()), 'requirement': requirement},
                                         ensure_ascii=False), encoding='utf-8')
output = Path(args[args.index('--output-last-message') + 1])
if mode != 'missing-result':
    output.write_text('x' * (256 * 1024 + 1) if mode == 'result-budget' else json.dumps(result, ensure_ascii=False), encoding='utf-8')
if handoff.get('qualificationChallenges'):
    evidence = home / ('native-evidence-' + session + '.jsonl')
    with evidence.open('a', encoding='utf-8') as stream:
        for index, challenge in enumerate(handoff['qualificationChallenges']):
            identifier = str(index)
            stream.write(json.dumps({'payload': {'type': 'function_call', 'name': 'exec_command',
                'call_id': identifier, 'arguments': json.dumps({'cmd': challenge['command']})}}) + '\n')
            stream.write(json.dumps({'payload': {'type': 'function_call_output', 'call_id': identifier,
                'output': 'Process exited with code 1; synthetic local fixture refusal'}}) + '\n')
events = [{'type': 'thread.started', 'thread_id': session}, {'type': 'turn.started'},
          {'type': 'item.completed', 'item': {'id': '0', 'type': 'agent_message', 'text': '中文🙂'}},
          {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}]
if mode == 'missing-terminal':
    events.pop()
if mode == 'wrong-session':
    events[0]['thread_id'] = 'not-a-native-uuid'
if mode == 'turn-failed':
    events[-1] = {'type': 'turn.failed', 'error': {'message': 'synthetic failure'}}
data = ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in events).encode('utf-8')
if mode in {'bad-utf8', 'bad-json', 'frame-budget', 'log-budget'}:
    data = {'bad-utf8': b'\xff\n', 'bad-json': b'{no}\n', 'frame-budget': b'x' * 65537,
            'log-budget': b''}[mode]
    if mode == 'log-budget':
        line = json.dumps({'type': 'item.completed', 'item': {'id': '0', 'type': 'agent_message', 'text': 'x' * 40000}}).encode() + b'\n'
        data = b''.join(json.dumps(item).encode() + b'\n' for item in events[:2]) + line * 30
for offset in range(0, len(data), 3):
    sys.stdout.buffer.write(data[offset:offset + 3])
    sys.stdout.buffer.flush()
if mode == 'nonzero-exit':
    raise SystemExit(7)
