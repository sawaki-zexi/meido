"""Opt-in only: covers the primary with an opaque fixture before capture.

Run alone on an interactive Windows 10/11 desktop:
  $env:MEIDO_SCREEN_NATIVE='1'; python -m pytest tests/python/test_screen_perception_native.py -q -s
No image is written, and the only vision destination is a local HTTP fixture.
"""

import asyncio
import base64
from io import BytesIO
import json
import os
import platform
import sys
import time

from PIL import Image, __version__ as pillow_version
import pytest

from backend.app.ai.model_adapter_registry import ModelAdapterRegistry, RoutedModelAdapter
from backend.app.screen_perception import ObserveScreenTool, ScreenPerception
from backend.app.screen_perception.worker import launch_capture_worker
from test_screen_perception import observation_host, vision_server


@pytest.mark.skipif(os.getenv("MEIDO_SCREEN_NATIVE") != "1", reason="requires explicit controlled-primary native opt-in")
def test_real_windows_primary_is_covered_by_controlled_marker_before_capture(tmp_path):
    assert sys.platform == "win32", "explicit native opt-in requires Windows"
    import ctypes
    import tkinter

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous_dpi = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    assert previous_dpi, "cannot establish controlled-window DPI context"
    width, height = user.GetSystemMetrics(0), user.GetSystemMetrics(1)
    assert width > 0 and height > 0
    window = None
    processes = []
    colors = ["#e7112a", "#19cb47", "#294be3", "#efd127"]
    try:
        window = tkinter.Tk()
        window.overrideredirect(True)
        window.geometry(f"{width}x{height}+0+0")
        window.attributes("-topmost", True)
        canvas = tkinter.Canvas(window, width=width, height=height, highlightthickness=0, borderwidth=0)
        canvas.pack(fill="both", expand=True)
        for x, y, color in [(0, 0, colors[0]), (width // 2, 0, colors[1]), (0, height // 2, colors[2]), (width // 2, height // 2, colors[3])]:
            canvas.create_rectangle(x, y, x + width // 2 + 1, y + height // 2 + 1, fill=color, outline=color)
        window.update()
        assert window.winfo_width() == width and window.winfo_height() == height
        window.lift()
        window.focus_force()
        window.update()
        time.sleep(0.2)
        window.update()
        from ctypes import wintypes

        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetAncestor.restype = wintypes.HWND
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = wintypes.HWND
        fixture_handle = user.GetAncestor(window.winfo_id(), 2)

        def assert_covered():
            # Check the actual top window at a grid of primary-screen points,
            # immediately before launching the real capture process.
            for x in (1, width // 4, width // 2, width * 3 // 4, width - 2):
                for y in (1, height // 4, height // 2, height * 3 // 4, height - 2):
                    visible = user.WindowFromPoint(wintypes.POINT(x, y))
                    assert user.GetAncestor(visible, 2) == fixture_handle, "primary marker is obscured; refusing capture"
        assert_covered()
        with vision_server() as (url, requests):
            _, _, context, roles, models, sessions, _ = observation_host(tmp_path, url)

            async def launch():
                assert_covered()
                process = await launch_capture_worker()
                processes.append(process)
                return process

            service = ScreenPerception(role_getter=roles.get, configuration_getter=models.get, sessions=sessions,
                                       adapter=RoutedModelAdapter(ModelAdapterRegistry(timeout=12)), launch_capture=launch)
            tool = ObserveScreenTool(service, roles.get(context.role_id).agentConfig.screenPerception.visionModelConfigurationId, role_id=context.role_id)
            result = asyncio.run(tool.execute({}, context))
            async def cancel_native_capture():
                task = asyncio.create_task(tool.execute({}, context))
                while len(processes) < 2:
                    await asyncio.sleep(0.001)
                started = time.monotonic()
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert processes[1].returncode is not None
                return round((time.monotonic() - started) * 1000)
            cancel_ms = asyncio.run(cancel_native_capture())
        assert result.is_error is False, result.content
        assert processes and processes[0].returncode == 0
        assert len(requests) == 1, "cancelled native capture must not upload another frame"
        blocks = requests[0][1]["messages"][-1]["content"]
        encoded = next(item for item in blocks if item["type"] == "image_url")["image_url"]["url"].split(",", 1)[1]
        with Image.open(BytesIO(base64.b64decode(encoded))) as frame:
            expected = [(231, 17, 42), (25, 203, 71), (41, 75, 227), (239, 209, 39)]
            actual = [frame.getpixel((frame.width * x // 4, frame.height * y // 4)) for x, y in [(1, 1), (3, 1), (1, 3), (3, 3)]]
            assert actual == expected, "captured image did not match the controlled primary marker"
            assert abs(frame.width / frame.height - width / height) < 0.01
        print(json.dumps({"os": platform.platform(), "python": platform.python_version(), "pillow": pillow_version,
                          "primary": [width, height], "monitors": user.GetSystemMetrics(80),
                          "dpi": user.GetDpiForWindow(fixture_handle), "cancelMs": cancel_ms,
                          "tool": json.loads(result.content), "resources": "success and cancelled workers terminal; local vision response closed", "metadata": result.details}))
        assert not list(tmp_path.rglob("*.png"))
    finally:
        if window is not None:
            window.destroy()
        user.SetThreadDpiAwarenessContext(previous_dpi)
