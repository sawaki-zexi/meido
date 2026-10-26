from dataclasses import dataclass

from .config import MAX_TEXT


class CallbackError(ValueError):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


@dataclass(frozen=True)
class BotMessage:
    message_id: str
    text: str
    reply_to: str = ''

    @classmethod
    def parse(cls, payload):
        if not isinstance(payload, dict) or set(payload) - {'message_id', 'text', 'reply_to'}:
            raise CallbackError(400, '消息字段无效')
        values = {}
        for key, limit in (('message_id', 128), ('text', MAX_TEXT), ('reply_to', 128)):
            value = payload.get(key, '')
            if (not isinstance(value, str) or len(value) > limit
                    or key != 'reply_to' and not value.strip()):
                raise CallbackError(400, '消息字段无效或过长')
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise CallbackError(400, '消息文字编码无效') from None
            values[key] = value if key == 'text' else value.strip()
        return cls(**values)
