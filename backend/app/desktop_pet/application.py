"""One application owns the desktop companion; model turns own only actions."""
import asyncio
from collections import deque
from pathlib import Path
import threading
import uuid
import sys

from .store import PetStore


class DesktopPetApplication:
    def __init__(self, roles, root: Path, *, enabled=lambda: True):
        self.store = PetStore(roles, root)
        self.host = None
        self.closed = False
        self._serial = asyncio.Lock()
        self._pending = set()
        self._host_binding = None
        self._events = deque(maxlen=64)
        self._event_gate = threading.RLock()
        self._error = ''
        self._enabled = enabled

    @property
    def has_live_owner(self):
        return self.host is not None and not self.host.closed

    async def _owned(self, function, *arguments, **keywords):
        task = asyncio.create_task(asyncio.to_thread(function, *arguments, **keywords))
        self._pending.add(task)
        cancelled = False
        try:
            while True:
                try:
                    value = await asyncio.shield(task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    if task.done():
                        value = task.result()
                        break
            if cancelled:
                raise asyncio.CancelledError
            return value
        finally:
            self._pending.discard(task)

    def status(self, role_id):
        value = self.store.status(role_id)
        value['host'] = self.host.snapshot() if self.host is not None else {'status': 'stopped', 'reason': self._error}
        with self._event_gate:
            value['history'] = [dict(event) for event in self._events if event.get('roleId') == role_id]
        value['unavailableReason'] = self.unavailable_reason(role_id)
        return value

    def _event(self, event):
        with self._event_gate:
            self._events.append(event)

    def unavailable_reason(self, role_id):
        if not self._enabled():
            return '桌宠插件已关闭'
        role, package, active = self.store.binding(role_id)
        if not role['enabled']:
            return '角色桌宠已关闭'
        if package is None or active != role_id:
            return '尚未绑定当前角色桌宠'
        if sys.platform != 'win32':
            return '桌宠宿主当前仅支持 Windows'
        if (self.host is None or self._host_binding != (role_id, role['generation'])
                or self.host.snapshot()['status'] != 'ready'):
            return self._error or '桌宠宿主未就绪；请构建宿主并重启桌宠'
        return ''

    async def start(self):
        async with self._serial:
            role_id = self.store.active_role()
            if role_id is not None:
                role, package, _ = self.store.binding(role_id)
                if role['enabled'] and package is not None:
                    # Rebuilding the bridge never reuses a prior run's authority.
                    await self._owned(self.store.configure, role_id, expected_revision=role['revision'],
                        package_id=package['id'], enabled=True)
            await self._refresh()

    async def import_package(self, role_id, raw):
        if self.closed:
            raise ValueError('桌宠服务已关闭')
        await self._owned(self.store.import_package, role_id, raw)
        return self.status(role_id)

    async def configure(self, role_id, *, expected_revision, package_id, enabled):
        if self.closed:
            raise ValueError('桌宠服务已关闭')
        # Commit revocation before waiting for an already dispatched action.
        await self._owned(self.store.configure, role_id, expected_revision=expected_revision,
            package_id=package_id, enabled=enabled)
        async with self._serial:
            if self.closed:
                raise ValueError('桌宠服务已关闭')
            await self._refresh()
            return self.status(role_id)

    async def _refresh(self):
        role_id = self.store.active_role()
        role, package, active = self.store.binding(role_id) if role_id is not None else (None, None, None)
        key = (role_id, role['generation']) if self._enabled() and role and role['enabled'] and package and active == role_id else None
        if self.host is not None and key != self._host_binding:
            await self._owned(self.host.close)
            self.host = None
            self._host_binding = None
        if key is not None and self.host is None:
            try:
                binding = await self._owned(self.store.native_binding, role_id)
            except (OSError, ValueError, KeyError) as error:
                self._error = str(error)[:256]
                return
            if binding is None:
                return
            from .host import PetHost
            self.host = PetHost(self.store.root / 'runtime', on_event=self._event)
            self._host_binding = key
            try:
                await self._owned(self.host.start, binding)
                self._error = ''
            except (OSError, ValueError, RuntimeError) as error:
                self._error = str(error)[:256]
                await self._owned(self.host.close)

    async def restart(self, role_id):
        async with self._serial:
            role, package, active = self.store.binding(role_id)
            if not self._enabled() or not role['enabled'] or package is None or active != role_id or self.closed:
                raise ValueError('请先启用当前角色桌宠')
            # Rotating the binding rejects tools frozen before this restart.
            await self._owned(self.store.configure, role_id, expected_revision=role['revision'],
                package_id=package['id'], enabled=True)
            await self._refresh()
            return self.status(role_id)

    async def invalidate(self, role_id):
        await self._owned(self.store.revoke, role_id)
        async with self._serial:
            await self._refresh()

    async def plugin_changed(self):
        """Rotate old actions while retaining role/package preferences."""
        async with self._serial:
            role_id = self.store.active_role()
            if role_id is not None:
                role, package, _ = self.store.binding(role_id)
                await self._owned(self.store.configure, role_id, expected_revision=role['revision'],
                    package_id=package['id'] if package else None, enabled=role['enabled'])
            await self._refresh()

    async def remove_package(self, role_id, package_id, expected_revision):
        async with self._serial:
            await self._owned(self.store.remove_package, role_id, package_id, expected_revision)
            return self.status(role_id)

    async def purge_role(self, role_id):
        if self._host_binding and self._host_binding[0] == role_id and self.has_live_owner:
            raise ValueError('桌宠原宿主尚未退出')
        await self._owned(self.store.purge, role_id)

    async def action(self, role_id, generation, arguments, context, *, authorized):
        async with self._serial:
            if self.closed or context.role_id != role_id or not authorized(context):
                raise ValueError('仅当前主人主动发起的角色桌面对话可控制桌宠')
            role, package, active = self.store.binding(role_id)
            if role['generation'] != generation or not role['enabled'] or active != role_id or package is None:
                raise ValueError('桌宠绑定已改变或许可已关闭')
            if context.signal.cancelled:
                raise ValueError('当前回合已停止')
            if arguments['action'] == 'play':
                if arguments.get('name') not in package['actions'] or set(arguments) != {'action', 'name'}:
                    raise ValueError('请选择当前包声明的动作')
            elif arguments['action'] == 'move':
                if (arguments.get('target') not in {'top_left', 'top_right', 'center', 'bottom_left', 'bottom_right'}
                        or arguments.get('animation', '') not in {'', 'idle', 'run'}
                        or set(arguments) - {'action', 'target', 'animation'}):
                    raise ValueError('桌宠移动参数无效')
            else:
                raise ValueError('桌宠动作无效')
            if self.unavailable_reason(role_id):
                raise ValueError(self.unavailable_reason(role_id))
            await self._owned(self.store.claim, role_id, generation, context.run_id)
            current, _, _ = self.store.binding(role_id)
            if current['generation'] != generation or not self._enabled() or not authorized(context) or context.signal.cancelled:
                raise ValueError('桌宠动作派发前许可已改变')
            def admit(send):
                return self.store.dispatch(role_id, generation,
                    lambda: not self.closed and self._enabled() and not context.signal.cancelled and authorized(context), send)
            return await self._owned(self.host.action, uuid.uuid4().hex, arguments, admit=admit)

    async def close(self):
        self.closed = True
        async with self._serial:
            if self._pending:
                await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._pending)), return_exceptions=True)
            if self.host is not None:
                await self._owned(self.host.close)
