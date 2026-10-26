import asyncio

from backend.app.agent_runtime.meido_provider import MeidoProvider
from backend.app.agent_runtime.provider import CancellationToken
from backend.app.ai.model_types import ModelAssistantMessage, ModelStreamDone, ModelTextBlock
from backend.app.models import RoleInput, RoleProfile
from backend.app.session_store import SessionStore
from backend.app.storage import initialize_databases


def test_partial_receipt_projects_real_rich_model_request_without_unsent_blocks(tmp_path):
    initialize_databases(tmp_path)
    sessions = SessionStore(tmp_path / 'sessions.db')
    sessions.append_message('thread:source', 'assistant', 'CONFIRMED UNSENT_TAIL', 'failed', metadata={
        'channel': 'telegram', 'deliveryStatus': 'uncertain', 'partialDelivery': True,
        'parts': [{'kind': 'text', 'content': 'CONFIRMED', 'status': 'sent', 'platformMessageId': 'real'},
                  {'kind': 'text', 'content': ' UNSENT_TAIL', 'status': 'uncertain', 'platformMessageId': None}],
        'content_blocks': [{'type': 'text', 'text': 'CONFIRMED UNSENT_TAIL', 'text_signature': None},
                           {'type': 'thinking', 'text': 'UNSENT_THINKING', 'signature': None, 'redacted': False},
                           {'type': 'image', 'media_type': 'image/png', 'data_base64': 'VU5TRU5UX0lNQUdF', 'detail': 'auto'}],
        'thinking': [{'text': 'UNSENT_THINKING'}],
    })
    requests = []
    class Adapter:
        async def stream_rich_messages(self, messages, configuration, tools=(), **kwargs):
            requests.extend(messages)
            yield ModelStreamDone(ModelAssistantMessage((ModelTextBlock('ok'),)))
    role = RoleInput(name='fixture', profile=RoleProfile(profile='fixture')).model_copy(update={'id': 'r'})
    provider = MeidoProvider(Adapter(), role, None)
    async def scenario():
        async for _ in provider.stream_response(model='', system='', messages=sessions.runtime_messages('thread:source'),
                tools=[], signal=CancellationToken()):
            pass
    asyncio.run(scenario())
    sent = [block.text for message in requests for block in message.content if hasattr(block, 'text')]
    assert 'CONFIRMED' in sent
    assert not any('UNSENT_' in text for text in sent)
    assert not any(block.type == 'image' for message in requests for block in message.content)
    original = sessions.list_messages('thread:source')[0]
    assert original.content == 'CONFIRMED UNSENT_TAIL' and len(original.metadata['content_blocks']) == 3
