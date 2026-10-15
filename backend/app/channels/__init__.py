"""Channel adapters used by the multi-channel conversation host.

The package deliberately keeps platform SDKs out of the host.  Adapters turn
platform payloads into the small, trusted event model below and send already
authorised messages through an injected transport.
"""

from .base import ChannelAdapter, ChannelAdapterError, UnsupportedTargetError
from .models import (
    ChatType,
    ChannelCapabilities,
    DeliveryResult,
    DeliveryStatus,
    InboundEvent,
    OutboundMessage,
    Target,
    TargetKind,
    target_from_chat,
)
from .napcat import NapCatAdapter, NapCatConfig
from .qqbot import QQBotAdapter, QQBotConfig
from .feishu import FeishuAdapter, FeishuConfig
from .host import ChannelHost, target_payload
from .transports import (
    ChannelTransportError,
    FeishuWebhookTransport,
    FeishuLongConnectionRunner,
    feishu_sdk_connection_factory,
    NapCatWebSocketTransport,
    QQBotGatewayTransport,
)
from .telegram import TelegramAccountAdapter
from .tools import ChannelAccountListTool, ChannelSendTool, ChannelTargetsTool
from .group_tools import (
    LookupGroupContextTool,
    LookupGroupMemberTool,
    MultichannelGroupRepository,
    SetGroupListeningTool,
    UpdateGroupContextTool,
)

__all__ = [
    "ChannelAdapter",
    "ChannelAdapterError",
    "UnsupportedTargetError",
    "ChatType",
    "ChannelCapabilities",
    "DeliveryResult",
    "DeliveryStatus",
    "InboundEvent",
    "OutboundMessage",
    "Target",
    "TargetKind",
    "target_from_chat",
    "NapCatAdapter",
    "NapCatConfig",
    "QQBotAdapter",
    "QQBotConfig",
    "FeishuAdapter",
    "FeishuConfig",
    "ChannelTransportError",
    "NapCatWebSocketTransport",
    "QQBotGatewayTransport",
    "FeishuWebhookTransport",
    "FeishuLongConnectionRunner",
    "feishu_sdk_connection_factory",
    "ChannelHost",
    "target_payload",
    "TelegramAccountAdapter",
    "ChannelAccountListTool",
    "ChannelTargetsTool",
    "ChannelSendTool",
    "MultichannelGroupRepository",
    "LookupGroupContextTool",
    "LookupGroupMemberTool",
    "SetGroupListeningTool",
    "UpdateGroupContextTool",
]
