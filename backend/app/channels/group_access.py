"""Recheck the real run source for structured group and member tools."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..agent_runtime.tools import ToolContext
from .source_authority import FrozenChannelSource, capture_channel_source

if TYPE_CHECKING:
    from ..multichannel import MultichannelStore


class GroupToolAccess:
    def __init__(self, store: MultichannelStore, *, role_id: str,
                 source_thread_id: str | None = None, is_owner: bool = True,
                 authorized: Callable[[ToolContext], bool] | None = None) -> None:
        self.store, self.role_id = store, role_id
        self.source_thread_id, self.is_owner, self.authorized = source_thread_id, is_owner, authorized
        self._sources: dict[str, FrozenChannelSource] = {}

    def source(self, context: ToolContext, *, owner: bool = False) -> FrozenChannelSource | None:
        if (context.signal.cancelled or context.role_id != self.role_id
                or self.authorized is not None and not self.authorized(context)):
            raise PermissionError("群资料来源无效、角色许可已撤销或调用已取消。")
        if self.source_thread_id in {None, f"desktop:{self.role_id}"}:
            if context.session_key != f"role:{self.role_id}" or not self.is_owner:
                raise PermissionError("群资料来源无效。")
            return None
        source = self._sources.get(context.run_id)
        if source is None:
            source = capture_channel_source(self.store, self.store.sessions,
                role_id=self.role_id, session_key=context.session_key,
                run_id=context.run_id, owner_context=self.is_owner)
            if source.thread_id != self.source_thread_id:
                raise PermissionError("群资料来源线程不符。")
            self._sources[context.run_id] = source
        if context.session_key != source.session_key or source.authorization_denial(self.store) is not None:
            raise PermissionError("群资料来源已失效或主人已解绑。")
        if owner and not source.owner_context:
            raise PermissionError("群笔记和旁听设置仅主人回合可用。")
        return source

    def member_scope(self, context: ToolContext) -> str | None:
        source = self.source(context)
        return None if source is None or source.owner_context and source.chat_type == "private" else source.thread_id

    def owner(self, context: ToolContext) -> None:
        self.source(context, owner=True)
