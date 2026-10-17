from dataclasses import replace

from ..agent_runtime.types import ToolResultMessage


def guard_provider(inner, host, *, role_id, run_id):
    class GuardedProvider:
        def stream_response(self, **request):
            async def stream():
                host.bind_run_signal(role_id, run_id, request["signal"])
                if not host.images_authorized(role_id, run_id):
                    request["messages"] = [replace(m, images=()) if isinstance(m, ToolResultMessage)
                        and m.tool_name.startswith("computer_") and m.images else m for m in request["messages"]]
                async for event in inner.stream_response(**request):
                    yield event
            return stream()
    return GuardedProvider()
