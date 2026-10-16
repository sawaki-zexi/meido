from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass, field, is_dataclass
from io import BytesIO
import json
import math
from pathlib import Path
from time import monotonic
from uuid import uuid4

from PIL import Image

from ..agent_runtime.tools import AgentToolResult
from ..agent_runtime.provider import CancellationToken
from ..agent_runtime.types import ImageContent


class ComputerUseError(RuntimeError):
    def __init__(self, code: str, *, effect: str = "not_started"):
        self.code, self.effect = code, effect
        super().__init__(code)


@dataclass
class _Run:
    role_id: str
    run_id: str
    generation: int
    configuration_id: str
    configuration: object
    driver: object = None
    lease: object = None
    task: asyncio.Task | None = None
    watcher: asyncio.Task | None = None
    closing: asyncio.Task | None = None
    started: float = 0
    touched: float = 0
    revoked: bool = False
    discovered: dict = field(default_factory=dict)
    observation: dict | None = None
    context: object = None
    acquiring: asyncio.Task | None = None

def _identity(value):
    return asdict(value) if is_dataclass(value) else vars(value).copy()


def _error(code, effect="not_started", input_receipt=None):
    details: dict[str, object] = {"status": code, "effect": effect}
    if input_receipt is not None:
        details["input_receipt"] = input_receipt
    return AgentToolResult(json.dumps(details), details, is_error=True)


def _input_receipt(value: object) -> dict[str, object] | None:
    """Copy only the bounded, non-content input facts from the pinned driver."""
    if (not isinstance(value, dict)
            or set(value) != {"status", "foreground", "business_effect", "business", "restore"}
            or value["status"] != "available"
            or not isinstance(value["business_effect"], str)
            or value["business_effect"] not in {"not_started", "unknown"}):
        return None
    stages = value["foreground"]
    if not isinstance(stages, list) or len(stages) > 8:
        return None
    copied = []
    names = {"click", "keyboard", "focus_click", "scroll"}
    counts = {"checks", "already_foreground_checks", "set_foreground_calls", "assisted_attempts"}
    flags = {"vk_noname_down_api_called", "vk_noname_up_api_called", "activated"}
    for stage in stages:
        if (not isinstance(stage, dict) or set(stage) != {"stage"} | counts | flags
                or not isinstance(stage["stage"], str) or stage["stage"] not in names - {"scroll"}
                or any(type(stage[key]) is not int for key in counts)
                or any(type(stage[key]) is not bool for key in flags)):
            return None
        if (not 0 <= stage["assisted_attempts"] <= 3
                or stage["checks"] != 1 + stage["assisted_attempts"]
                or not 0 <= stage["already_foreground_checks"] <= stage["checks"]
                or stage["set_foreground_calls"] != stage["checks"] - stage["already_foreground_checks"]):
            return None
        copied.append({"stage": stage["stage"], **{key: stage[key] for key in counts | flags}})
    business, restores = value["business"], value["restore"]
    if (not isinstance(business, list) or len(business) > 8
            or not isinstance(restores, list) or len(restores) > 8):
        return None
    safe_business = []
    for item in business:
        if (not isinstance(item, dict) or set(item) != {"stage", "input_api_called"}
                or not isinstance(item["stage"], str) or item["stage"] not in names
                or type(item["input_api_called"]) is not bool):
            return None
        if item["input_api_called"] and value["business_effect"] != "unknown":
            return None
        safe_business.append({"stage": item["stage"], "input_api_called": item["input_api_called"]})
    safe_restores = []
    for item in restores:
        if (not isinstance(item, dict)
                or set(item) != {"stage", "status", "set_foreground_calls", "confirmed"}
                or not isinstance(item["stage"], str) or item["stage"] not in names - {"scroll"}
                or not isinstance(item["status"], str) or item["status"] not in {"available", "pending"}):
            return None
        if item["status"] == "pending":
            if item["set_foreground_calls"] is not None or item["confirmed"] is not None:
                return None
        else:
            if type(item["set_foreground_calls"]) is not int or not 0 <= item["set_foreground_calls"] <= 1:
                return None
            if type(item["confirmed"]) is not bool and not (
                item["set_foreground_calls"] == 0 and item["confirmed"] is None
            ):
                return None
        safe_restores.append({key: item[key] for key in ("stage", "status", "set_foreground_calls", "confirmed")})
    return {"status": "available", "foreground": copied, "business_effect": value["business_effect"],
            "business": safe_business, "restore": safe_restores}


def _received_input_receipt(result, structured):
    invalid = {"status": "unavailable", "reason": "invalid"}
    present = "input_receipt" in structured
    value = structured.get("input_receipt")
    blocks = result.get("content", [])
    if not isinstance(blocks, list) or len(blocks) > 8:
        return invalid

    def unique_object(pairs):
        values = {}
        for key, item in pairs:
            if key in values:
                raise ValueError("duplicate receipt carrier key")
            values[key] = item
        return values

    marker, carriers = "meido_input_receipt_v1", []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        if len(text) > 8192:
            return invalid
        try:
            carrier = json.loads(text, object_pairs_hook=unique_object)
        except (ValueError, TypeError, RecursionError):
            if marker in text:
                return invalid
            continue
        if isinstance(carrier, dict) and marker in carrier:
            if set(carrier) != {marker}:
                return invalid
            carriers.append(carrier[marker])
    # An explicit structured field is never silently replaced, even by an
    # equal carrier. Two representations or two carriers are ambiguous.
    if len(carriers) > 1 or present and carriers:
        return invalid
    if carriers:
        present, value = True, carriers[0]
    return _input_receipt(value) or {"status": "unavailable", "reason": "invalid" if present else "missing"}


class ComputerUseHost:
    def __init__(self, *, role_getter, configuration_getter, sessions, model_registry,
                 resources: Path, data_root: Path, role_unavailable=lambda _: False,
                 desktop=None, driver_factory=None, lease_factory=None, clock=monotonic):
        self._role, self._configuration, self._sessions = role_getter, configuration_getter, sessions
        self._registry, self._resources, self._root = model_registry, resources, data_root
        self._unavailable, self._desktop_port = role_unavailable, desktop
        self._driver_factory, self._lease_factory, self._clock = driver_factory, lease_factory, clock
        self._runs: dict[tuple[str, str], _Run] = {}
        self._generations: dict[str, int] = {}
        self._run_signals: dict[tuple[str, str], CancellationToken] = {}
        self._owner = None
        self._closed = False

    @property
    def desktop(self):
        if self._desktop_port is None:
            from .windows import WindowsDesktop
            self._desktop_port = WindowsDesktop()
        return self._desktop_port

    def generation(self, role_id):
        return self._generations.get(role_id, 0)

    def enabled_for(self, role):
        return bool(getattr(getattr(role.agentConfig, "computerUse", None), "enabled", False))

    def dependency_status(self, role):
        if not self.enabled_for(role):
            return {"status": "unavailable", "reason": "角色的 Computer Use 开关已关闭"}
        try:
            configuration = self._configuration(role.modelConfigurationId)
            if configuration is None or not self._registry.supports_images(configuration):
                return {"status": "unavailable", "reason": "当前聊天模型连接不能编码窗口图片"}
            if self._driver_factory is None:
                from .driver import resolve_driver
                resolve_driver(self._resources)
            return {"status": "unchecked", "reason": "桌面身份、解锁状态和窗口交付将在调用时验证"}
        except (OSError, ValueError, RuntimeError):
            return {"status": "unavailable", "reason": "固定 Meido Computer Use 驱动或当前平台不可用"}

    def _validate(self, context, expected_generation=None, *, check_signal=True):
        if check_signal and context.signal.cancelled:
            raise asyncio.CancelledError
        role = self._role(context.role_id)
        if (self._closed or role is None or self._unavailable(context.role_id) or not self.enabled_for(role)
                or expected_generation is not None and expected_generation != self.generation(context.role_id)):
            raise ComputerUseError("permission_denied")
        sessions = self._sessions() if callable(self._sessions) else self._sessions
        run = sessions.get_run(context.run_id)
        if (context.session_key != f"role:{context.role_id}" or run is None or run.roleId != context.role_id
                or run.sessionKey != context.session_key or run.status not in {"created", "running"}
                or run.modelSnapshot.get("computerUseSource") != "desktop_owner"):
            raise ComputerUseError("permission_denied")
        users = [m for m in sessions.list_messages(context.session_key) if m.runId == context.run_id and m.role == "user"]
        if (len(users) != 1 or users[0].status != "completed" or users[0].messageType != "text"
                or users[0].metadata.get("channel", "desktop") != "desktop"
                or any(users[0].metadata.get(k) for k in ("scheduled", "background", "taskId", "proactive", "trigger", "senderId"))):
            raise ComputerUseError("permission_denied")
        configuration = self._configuration(role.modelConfigurationId)
        if configuration is None or not self._registry.supports_images(configuration):
            raise ComputerUseError("vision_unsupported")
        binding = configuration.id
        if run.modelConfigurationId and run.modelConfigurationId != binding:
            raise ComputerUseError("configuration_changed")
        owned = self._runs.get((context.role_id, context.run_id))
        if owned is not None and (owned.revoked or owned.generation != self.generation(context.role_id)
                                   or owned.configuration_id != binding or owned.configuration != configuration):
            raise ComputerUseError("run_released" if owned.revoked else "configuration_changed")
        self.desktop.validate()
        return configuration

    async def call(self, name, arguments, context, expected_generation):
        started = False
        owned = None
        received_receipt: dict[str, object] | None = None
        try:
            configuration = self._validate(context, expected_generation)
            key = (context.role_id, context.run_id)
            if self._owner is not None and self._owner != key:
                raise ComputerUseError("busy")
            owned = self._runs.get(key)
            if name == "release":
                if owned is None:
                    owned = _Run(*key, self.generation(context.role_id), configuration.id, configuration)
                    self._runs[key] = owned
                await self.close_run(*key)
                return AgentToolResult("Computer Use 控制已释放，本回合不能重新输入。", {"status": "released"})
            if owned is None:
                owned = _Run(*key, self.generation(context.role_id), configuration.id, configuration)
                self._runs[key] = owned
            owned.context = context
            if owned.task is not None or owned.closing is not None:
                raise ComputerUseError("busy")
            self._owner = key
            owned.touched = self._clock()
            if not owned.started:
                owned.started = owned.touched
            owned.task = asyncio.current_task()
            if name == "get_window_state":
                # Even a rejected state attempt must not revive a previous
                # window's PID-scoped native resize state.
                owned.observation = None
            if owned.watcher is None:
                owned.watcher = asyncio.create_task(self._watch(owned, context))
            async with asyncio.timeout(30):
                if owned.driver is None:
                    from .lease import DesktopLease
                    async def acquire():
                        owned.lease = await asyncio.to_thread(self._lease_factory or DesktopLease)
                    owned.acquiring = asyncio.create_task(acquire())
                    await asyncio.shield(owned.acquiring)
                    if self._driver_factory is None:
                        from .driver import PinnedDriverSession
                        owned.driver = PinnedDriverSession(self._resources, self._root / uuid4().hex)
                    else:
                        owned.driver = self._driver_factory(owned)
                    await owned.driver.connect()
                self._validate(context, expected_generation)
                native = dict(arguments)
                target = None
                if name != "list_windows":
                    key_window = (arguments["pid"], arguments["window_id"])
                    target = _identity(self.desktop.inspect(*key_window))
                    discovered = owned.discovered.get(key_window)
                    if discovered is None or discovered[0] != target or self._clock() - discovered[1] > 30:
                        raise ComputerUseError("stale_window")
                    if name == "get_window_state":
                        native.setdefault("max_depth", 6)
                        native.setdefault("max_elements", 200)
                        native.setdefault("max_dimension", 1600)
                    else:
                        self._validate_action(owned, target, arguments, name)
                        native.pop("observation_id", None)
                        if not native.get("element_token"):
                            native.pop("snapshot_id", None)
                        # Every attempted action consumes the only actionable receipt.
                        owned.observation = None
                started = True
                result = await owned.driver.call(name, native)
                if name not in {"list_windows", "get_window_state"}:
                    received = self._structured(result)
                    received_receipt = _received_input_receipt(result, received)
                self._validate(context, expected_generation)
                if name == "list_windows":
                    if result.get("isError"):
                        return AgentToolResult(json.dumps(self._structured(result))[:65536],
                                               {"status": "rejected", "effect": "not_started"}, is_error=True)
                    data = self._structured(result)
                    windows = data.get("windows", []) if isinstance(data, dict) else []
                    admitted = []
                    owned.discovered.clear()
                    for item in windows[:100]:
                        try:
                            identity = _identity(self.desktop.inspect(item["pid"], item["window_id"]))
                        except (OSError, ValueError, KeyError):
                            continue
                        owned.discovered[(item["pid"], item["window_id"])] = (identity, self._clock(), uuid4().hex)
                        admitted.append(item)
                    return AgentToolResult(json.dumps({"windows": admitted}, ensure_ascii=False), {"status": "observed"})
                if name == "get_window_state":
                    return self._observe(owned, target, result, context)
                data = dict(self._structured(result))
                failed = result.get("isError", False)
                if failed and data.get("code") in (None, "native_error", "tool_invocation_failed"):
                    code = "native_error"
                    for block in result.get("content", [])[:8]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            if isinstance(text, str) and text.partition(":")[0] == "foreground_unavailable":
                                code = "foreground_unavailable"
                                break
                    if "code" not in data:
                        data["code"] = code
                    elif code == "foreground_unavailable":
                        data["cause_code"] = code
                # Driver acceptance is not verification of application effects.
                details: dict[str, object] = {"status": "rejected" if failed else "input_dispatched", "effect": "unknown"}
                details["input_receipt"] = received_receipt
                data["input_receipt"] = received_receipt
                return AgentToolResult(json.dumps(data, ensure_ascii=False)[:65536],
                    details, is_error=failed)
        except ComputerUseError as error:
            return _error(error.code, "unknown" if started else error.effect, received_receipt)
        except asyncio.CancelledError:
            if owned:
                owned.task = None
                await self.close_run(owned.role_id, owned.run_id)
            raise
        except BaseException as error:
            if owned:
                owned.task = None
                await self.close_run(owned.role_id, owned.run_id)
            if not isinstance(error, Exception):
                raise
            code = "timeout" if isinstance(error, TimeoutError) else getattr(error, "code", "driver_unavailable")
            if started and name not in {"list_windows", "get_window_state"} and received_receipt is None:
                received_receipt = {"status": "unavailable", "reason": "response_missing"}
            return _error(code, "unknown" if started else "not_started", received_receipt)
        finally:
            if owned:
                owned.task = None

    @staticmethod
    def _structured(result):
        data = result.get("structuredContent")
        if isinstance(data, dict):
            return data
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    value = json.loads(block.get("text", ""))
                    if isinstance(value, dict):
                        if "meido_input_receipt_v1" in value:
                            continue
                        return value
                except (ValueError, TypeError, RecursionError):
                    pass
        return {}

    def _observe(self, owned, target, result, context):
        data = dict(self._structured(result))
        if data.get("pid") != target["pid"] or data.get("window_id") != target["window_id"] or not isinstance(data.get("snapshot_id"), str):
            raise ComputerUseError("invalid_observation")
        if _identity(self.desktop.inspect(target["pid"], target["window_id"])) != target:
            raise ComputerUseError("stale_window")
        images = []
        capture = data.get("capture") or {}
        for block in result.get("content", []):
            if block.get("type") != "image":
                continue
            if images or capture.get("provenance") not in {"print_window", "wgc"} or capture.get("source_window_id") != target["window_id"]:
                raise ComputerUseError("invalid_capture_provenance")
            self._validate_capture(capture)
            encoded = block.get("data", "")
            if not isinstance(encoded, str) or len(encoded) > 2796204:
                raise ComputerUseError("image_budget_exceeded")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > 2 * 1024 * 1024:
                raise ComputerUseError("image_budget_exceeded")
            with Image.open(BytesIO(raw)) as image:
                if image.format != "PNG" or max(image.size) > 1600 or image.width * image.height > 2560000 or list(image.size) != capture.get("image_dimensions"):
                    raise ComputerUseError("invalid_capture_geometry")
                image.verify()
            images.append(ImageContent("image/png", encoded, detail="high"))
        data.pop("screenshot", None)
        receipt = uuid4().hex
        data["observation_id"] = receipt
        content = json.dumps(data, ensure_ascii=False)
        if len(content.encode()) > 65536:
            raise ComputerUseError("observation_budget_exceeded")
        owned.observation = {"identity": target, "snapshot_id": data["snapshot_id"], "observation_id": receipt,
            "tokens": {e["element_token"]: e.get("index", e.get("element_index")) for e in data.get("elements", []) if isinstance(e, dict) and e.get("element_token")},
            "capture": capture if images else {}, "at": self._clock(), "activity": self.desktop.activity()}
        return AgentToolResult(content, {"status": "observed"}, images=tuple(images))

    @staticmethod
    def _validate_capture(capture):
        source, image = capture.get("source_dimensions"), capture.get("image_dimensions")
        def dimensions(value):
            return (isinstance(value, list) and len(value) == 2
                    and all(type(v) is int and v > 0 for v in value))
        if (not dimensions(source) or not dimensions(image) or source[0] * source[1] > 16777216
                or image[0] > source[0] or image[1] > source[1]):
            raise ComputerUseError("invalid_capture_geometry")
        origin, ratio = capture.get("physical_origin"), capture.get("resize_ratio")
        if capture.get("pixel_coordinates_available") is True:
            if (not isinstance(origin, list) or len(origin) != 2 or any(type(v) is not int for v in origin)
                    or isinstance(ratio, bool) or not isinstance(ratio, (float, int)) or not math.isfinite(ratio)
                    or ratio < 1 or abs(ratio - source[0] / image[0]) > 1e-6
                    or abs(image[1] * ratio - source[1]) > ratio + 1):
                raise ComputerUseError("invalid_capture_geometry")
        elif capture.get("pixel_coordinates_available") is not False or origin is not None or ratio is not None:
            raise ComputerUseError("invalid_capture_geometry")

    def _validate_action(self, owned, target, arguments, name):
        observed = owned.observation
        if (observed is None or observed["identity"] != target or self._clock() - observed["at"] > 30
                or arguments.get("snapshot_id") != observed["snapshot_id"] or arguments.get("observation_id") != observed["observation_id"]):
            raise ComputerUseError("stale_observation")
        token = arguments.get("element_token")
        has_pixel = "x" in arguments or "y" in arguments
        if token and has_pixel or "element_index" in arguments and not token:
            raise ComputerUseError("ambiguous_target")
        if token and (token not in observed["tokens"] or "element_index" in arguments and observed["tokens"][token] != arguments["element_index"]):
            raise ComputerUseError("stale_element")
        if has_pixel:
            capture = observed["capture"]
            if not capture.get("pixel_coordinates_available") or capture.get("physical_origin") is None or capture.get("resize_ratio") is None:
                raise ComputerUseError("pixel_mapping_unavailable")
            size = capture.get("image_dimensions", [0, 0])
            if "x" not in arguments or "y" not in arguments or not (0 <= arguments["x"] < size[0] and 0 <= arguments["y"] < size[1]):
                raise ComputerUseError("coordinates_out_of_bounds")
        if name in {"click", "type_text"} and not token and not has_pixel:
            raise ComputerUseError("explicit_target_required")
        if (name == "hotkey" or arguments.get("modifiers") or arguments.get("modifier")) and arguments.get("delivery_mode") != "foreground":
            raise ComputerUseError("background_unavailable")
        if self.desktop.activity() != observed["activity"] or observed["activity"][2]:
            raise ComputerUseError("foreground_conflict")
        if not self.desktop.input_enabled(target["window_id"]):
            raise ComputerUseError("target_disabled")

    async def _watch(self, owned, context):
        try:
            while not owned.revoked:
                await asyncio.sleep(.05)
                # Runtime cancels a tool-local token after every successful
                # call. The provider supplies the run token for model waits;
                # without a provider, watch the local token only while active.
                run_signal = self._run_signals.get((owned.role_id, owned.run_id))
                if (run_signal is not None and run_signal.cancelled
                        or run_signal is None and owned.task is not None and context.signal.cancelled):
                    await self.close_run(owned.role_id, owned.run_id)
                    return
                self._validate(context, owned.generation, check_signal=False)
                if self._clock() - owned.started >= 300 or self._clock() - owned.touched >= 90:
                    break
            if not owned.revoked:
                await self.close_run(owned.role_id, owned.run_id)
        except (ComputerUseError, OSError, ValueError):
            await self.close_run(owned.role_id, owned.run_id)
        except asyncio.CancelledError:
            if context.signal.cancelled and not owned.revoked:
                await self.close_run(owned.role_id, owned.run_id)

    async def close_run(self, role_id, run_id):
        owned = self._runs.get((role_id, run_id))
        if owned is None:
            return
        owned.revoked = True
        if owned.closing is None or owned.closing.done() and owned.closing.exception():
            owned.closing = asyncio.create_task(self._dispose(owned))
        from ..mcp_stdio import finish_cleanup
        await finish_cleanup(owned.closing)

    async def _dispose(self, owned):
        caller = asyncio.current_task()
        for task in (owned.task, owned.watcher):
            if task is not None and task is not caller and not task.done():
                task.cancel()
        if owned.acquiring is not None:
            try:
                await asyncio.shield(owned.acquiring)
            except Exception:
                # Failed nonblocking admission has no lease to release.
                if owned.lease is not None:
                    raise
        if owned.driver is not None:
            await owned.driver.close()
            if not owned.driver.exit_confirmed:
                raise ComputerUseError("cleanup_unconfirmed", effect="unknown")
        owned.observation = None
        owned.discovered.clear()
        owned.configuration = None
        if owned.lease is not None:
            await asyncio.to_thread(owned.lease.close)
            owned.lease = None
        if self._owner == (owned.role_id, owned.run_id):
            self._owner = None

    async def invalidate(self, *, role_id=None, configuration_id=None):
        roles = {role_id} if role_id is not None else {r.role_id for r in self._runs.values() if configuration_id is None or r.configuration_id == configuration_id}
        for rid in roles:
            self._generations[rid] = self.generation(rid) + 1
        await asyncio.gather(*(self.close_run(r.role_id, r.run_id) for r in tuple(self._runs.values()) if r.role_id in roles))

    async def close(self):
        self._closed = True
        await self.invalidate()
        self._runs.clear()
        self._run_signals.clear()

    async def finish_run(self, role_id, run_id):
        """Only the host's completed runtime boundary may forget revocation."""
        await self.close_run(role_id, run_id)
        self._runs.pop((role_id, run_id), None)
        self._run_signals.pop((role_id, run_id), None)

    def images_authorized(self, role_id, run_id):
        owned = self._runs.get((role_id, run_id))
        if not owned or owned.revoked or owned.context is None:
            return False
        signal = self._run_signals.get((role_id, run_id))
        if signal is not None and signal.cancelled:
            return False
        try:
            self._validate(owned.context, owned.generation, check_signal=False)
            return True
        except (ComputerUseError, OSError, ValueError):
            return False

    def bind_run_signal(self, role_id, run_id, signal):
        self._run_signals[(role_id, run_id)] = signal
