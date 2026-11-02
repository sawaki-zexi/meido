"""Local controlled embedding boundary for memory integration tests."""
import hashlib

from backend.app.memory_service import MemoryService
from backend.app.memory_maintenance import MemoryMaintenance


class ControlledMaintenance(MemoryMaintenance):
    """Explicit local extraction output for persistence-focused fixtures."""
    def __init__(self, *args, **kwargs):
        def extract(source, messages):
            return {'history': '\n'.join(message.content for message in messages),
                'pendingItems': [{'memoryType': 'fact', 'summary': message.content,
                    'sourceKey': f'{source}:{message.id}:fact'} for message in messages if message.role == 'user']}
        kwargs.setdefault('consolidation_provider', extract)
        super().__init__(*args, **kwargs)


class ControlledEmbedding:
    model_id = 'controlled-embedding'
    dimensions = 64
    ready = True
    is_enabled = True

    def embed(self, role_id, text):
        vector = [0.0] * self.dimensions
        for char in text:
            value = hashlib.sha256(char.encode()).digest()
            vector[value[0] % self.dimensions] += 1 if value[1] & 1 else -1
        return vector if any(vector) else [1.0] + [0.0] * (self.dimensions - 1)


def configured_service(store, provider=None, **kwargs):
    return MemoryService(store, provider or ControlledEmbedding(), **kwargs)


def conversation_service(store, **kwargs):
    """A configured local model, with explicit fixture outputs (no live calls)."""
    outputs = {
        '请记住我喜欢海边': '喜欢海边', '请记住我喜欢乌龙茶': '喜欢乌龙茶',
        '我喜欢红茶': '喜欢红茶', '我喜欢咖啡': '喜欢咖啡',
        '我以前喜欢红茶，现在喜欢咖啡': '喜欢咖啡',
        '以前喜欢咖啡，现在改为喜欢茶': '喜欢茶',
    }
    def provider(role, session, user, assistant, active):
        summary = outputs.get(user.content)
        return {'profile': [], 'preference': [{'summary': summary}] if summary else [], 'procedure': []}
    kwargs.setdefault('post_response_provider', provider)
    return configured_service(store, **kwargs)
