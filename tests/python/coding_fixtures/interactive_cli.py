"""No-network interactive Codex/Claude protocol fixture, never a real model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


kind = os.environ['MEIDO_HERDR_AGENT_KIND']
arguments = sys.argv[1:]
if '--version' in arguments:
    print('codex-cli 0.159.3' if kind == 'codex' else '2.1.283 (Claude Code)', flush=True)
    sys.exit(0)
if '--resume' in arguments:
    native = arguments[arguments.index('--resume') + 1]
elif 'resume' in arguments:
    native = arguments[arguments.index('resume') + 1]
else:
    native = str(uuid.uuid4())
sequence = time.time_ns() // 100


def report(state, session=False):
    global sequence
    sequence += 1
    command = [os.environ['HERDR_BIN_PATH'], '--session', os.environ['MEIDO_HERDR_SESSION'],
               'pane', 'report-agent-session' if session else 'report-agent', os.environ['HERDR_PANE_ID'],
               '--source', 'herdr:' + kind, '--agent', kind, '--seq', str(sequence), '--agent-session-id', native]
    if not session:
        command += ['--state', state]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True)
    if result.returncode:
        print('fixture hook failed', flush=True)
        sys.exit(3)


report('idle', session=True)
report('idle')
print('\x1b]0;' + ('Codex fixture' if kind == 'codex' else '✳ Claude fixture') + '\x07', flush=True)
print('› ' if kind == 'codex' else '❯ ', flush=True)
for line in sys.stdin:
    line = line.replace('\x1b[200~', '').replace('\x1b[201~', '').strip()
    if not line:
        continue
    report('working')
    if 'MEIDO_HANDOFF ' in line:
        handoff = json.loads(Path(line.rsplit('MEIDO_HANDOFF ', 1)[1]).read_text(encoding='utf-8'))
        if 'resultSchema' in handoff:
            writable = [Path(arguments[index + 1]).resolve() for index, value in enumerate(arguments) if value == '--add-dir']
            if writable != [Path(handoff['resultFile']).resolve().parent]:
                print('fixture: current attempt result write grant missing', flush=True)
                sys.exit(4)
        if handoff['requirement'] == 'hold until cancellation':
            time.sleep(3600)
        Path('fixture.txt').write_text(handoff['requirement'], encoding='utf-8')
        probe = handoff.get('qualificationChallenges')
        if probe:
            # A synthetic native transcript only: this fixture does not run
            # commands or prove the real Codex/Claude permission boundary.
            home = Path(os.environ['CODEX_HOME' if kind == 'codex' else 'CLAUDE_CONFIG_DIR'])
            transcript = home / ('fixture-session-' + native + '.jsonl')
            with transcript.open('a', encoding='utf-8') as stream:
                for challenge in probe:
                    call_id = 'fixture-' + challenge['id']
                    if kind == 'codex':
                        events = [{'payload': {'type': 'function_call', 'name': 'exec_command', 'call_id': call_id, 'arguments': json.dumps({'cmd': challenge['command']})}},
                                  {'payload': {'type': 'function_call_output', 'call_id': call_id, 'output': 'Process exited with code 1\nfixture denial only'}}]
                    else:
                        events = [{'sessionId': native, 'message': {'content': [{'type': 'tool_use', 'name': 'Write', 'id': call_id, 'input': {'file_path': challenge['outside']}}]}},
                                  {'sessionId': native, 'message': {'content': [{'type': 'tool_result', 'tool_use_id': call_id, 'is_error': True}]}}]
                    for event in events: stream.write(json.dumps(event) + '\n')
        Path(handoff['resultFile']).write_text(json.dumps({'task_id': handoff['taskId'], 'run_id': handoff['attemptId'],
            'status': 'completed', 'summary': handoff['requirement'], 'validation': ['native-probe'] if probe else ['fixture'], 'blockers': []}, ensure_ascii=False), encoding='utf-8')
        Path(handoff['resultFile'] + '.native').write_text(native, encoding='utf-8')
        print('fixture turn done', flush=True)
    report('idle')
