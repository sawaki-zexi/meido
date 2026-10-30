"""Pure input projection of prepared provider payloads, without transport/auth IO."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from ..models import ModelConfiguration


def project_input(payload: dict[str, Any], configuration: ModelConfiguration) -> dict[str, Any]:
    """Keep exact wire content; only give protocol-specific input fields common names.

    Connection/generation identity participates in comparison, not token cost.
    Credentials are never retained in the request snapshot or diagnostics.
    """
    value = deepcopy(payload)
    messages = value.pop('messages', value.pop('input', value.pop('contents', [])))
    if 'context' in value:
        messages = value.pop('context')['messages']
    for name in ('system', 'systemInstruction', 'instructions'):
        if name in value:
            messages.insert(0, {'role': 'system', 'content': value.pop(name)})
    tools = value.pop('tools', None)
    if 'toolConfig' in value:
        tools = value.pop('toolConfig')
    # Pi carries schema changes inside system messages. Keep those exact fields
    # in messages: changing the declaration still invalidates prefix equality.
    response_format = value.pop('response_format', None)
    if isinstance(value.get('text'), dict) and 'format' in value['text']:
        response_format = value['text']['format']
    for name in ('stream', 'stream_options', 'max_tokens', 'max_completion_tokens', 'max_output_tokens'):
        value.pop(name, None)
    for name, output_key in (('generationConfig', 'maxOutputTokens'), ('inferenceConfig', 'maxTokens'), ('options', 'maxTokens')):
        if isinstance(value.get(name), dict):
            value[name].pop(output_key, None)
            value[name].pop('stream', None)
            if not value[name]:
                del value[name]
    connection = {name: getattr(configuration, name) for name in
                  ('id', 'providerId', 'provider', 'api', 'baseUrl', 'apiKey', 'apiOptions', 'model')}
    result: dict[str, Any] = {'messages': messages, '_connection': hashlib.sha256(
        json.dumps(connection, sort_keys=True).encode()).hexdigest(), '_options': value}
    if tools is not None:
        result['tools'] = tools
    if response_format is not None:
        result['response_format'] = response_format
    return result
