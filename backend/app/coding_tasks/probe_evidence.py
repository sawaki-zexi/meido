"""Fail-closed interpretation of native CLI refusal evidence, never model claims."""
import json
from pathlib import Path


def native_refusals(home: Path, kind: str, session: str, challenges: list[dict]) -> dict:
    """Require matching actual tool calls AND CLI refusal results.

    Transcript locations/protocol are deliberately limited to the pinned CLI
    versions. Unknown formats cannot silently qualify a profile.
    """
    files = sorted(home.rglob('*.jsonl'))
    if len(files) > 1000:
        raise ValueError('native probe transcript file budget exceeded')
    calls, refused = {}, set()
    total = 0
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(home.resolve()):
            raise ValueError('native probe transcript is redirected')
        if kind == 'codex' and session not in path.name:
            continue
        with path.open('rb') as stream:
            while line := stream.readline(65537):
                total += len(line)
                if len(line) > 65536 or total > 1024 * 1024:
                    raise ValueError('native probe transcript exceeds budget')
                event = json.loads(line)
                if kind == 'codex':
                    payload = event.get('payload', {})
                    if payload.get('type') == 'function_call' and payload.get('name') == 'exec_command':
                        arguments = json.loads(payload.get('arguments', '{}'))
                        command = arguments.get('cmd')
                        for challenge in challenges:
                            if command == challenge.get('command'):
                                calls[payload.get('call_id')] = challenge['id']
                    elif payload.get('type') == 'function_call_output' and payload.get('call_id') in calls:
                        output = payload.get('output', '')
                        # These are the fixed CLI's process-result envelope,
                        # with a host-owned challenge exit code, not summary
                        # strings supplied in the CodingTask result JSON.
                        if isinstance(output, str) and ('Process exited with code 1' in output or 'Exit code: 1' in output):
                            refused.add(calls[payload['call_id']])
                else:
                    if event.get('sessionId') != session:
                        continue
                    for item in event.get('message', {}).get('content', []):
                        if not isinstance(item, dict):
                            continue
                        if item.get('type') == 'tool_use' and item.get('name') == 'Write':
                            for challenge in challenges:
                                if item.get('input', {}).get('file_path') == challenge.get('outside'):
                                    calls[item.get('id')] = challenge['id']
                        elif item.get('type') == 'tool_result' and item.get('tool_use_id') in calls and item.get('is_error') is True:
                            refused.add(calls[item['tool_use_id']])
    missing = {item['id'] for item in challenges} - refused
    if missing:
        raise ValueError('missing native CLI refusal evidence: ' + ','.join(sorted(missing)))
    return {'nativeSession': session, 'refusedChallenges': sorted(refused),
            'scope': 'fixed CLI permissions in a trusted repository; not a same-account OS sandbox'}
