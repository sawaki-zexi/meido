"""Image accounting at the real provider HTTP boundary, without a cloud model."""

import asyncio
import base64
from contextlib import aclosing
from contextlib import suppress
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import random
import struct
import threading
import zlib

from PIL import Image
import pytest

from backend.app.ai import (
    ModelAdapterRegistry, ModelImageBlock, ModelStreamDone, ModelUserMessage,
    RichModelStreamError, RoutedModelAdapter,
)
from backend.app.models import ModelConfiguration


@pytest.fixture
def local_model(local_platform_only):
    class Endpoint:
        url = ""
        bodies = []
        reservations = []
        reservations_at_arrival = []
        arrived = threading.Event()

    endpoint = Endpoint()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            endpoint.bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            endpoint.reservations_at_arrival.append(len(endpoint.reservations))
            endpoint.arrived.set()
            payload = json.loads(endpoint.bodies[-1])
            payload["_fixture_path"] = self.path
            data = endpoint.response(payload) if hasattr(endpoint, "response") else (
                'data: {"id":"local","choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
                'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'
            ).encode()
            self.send_response(getattr(endpoint, "status", 200))
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    endpoint.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, name="local-image-provider")
    thread.start()
    try:
        yield endpoint
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
        assert not thread.is_alive()


def fixture_image():
    output = io.BytesIO()
    # A normal-sized, self-made image; no owner files or remote media.
    Image.new("RGB", (1000, 600), (41, 83, 127)).save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def test_each_actual_request_reserves_every_repeated_image_before_arrival(local_model):
    from backend.app.ai.request_budget import ImageProof, image_request_budget

    data = fixture_image()
    block = ModelImageBlock("image/png", data)
    proof = ImageProof("image/png", hashlib.sha256(data.encode("ascii")).hexdigest(), len(data))
    configuration = ModelConfiguration(
        providerId="local", provider="local", api="openai-completions",
        baseUrl=local_model.url, model="local-vision", apiKey="synthetic-test-key",
    )
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))

    async def reserve(usage):
        if sum(item.image_occurrences for item in local_model.reservations) + usage.image_occurrences > 4:
            raise ValueError("image request budget exhausted")
        local_model.reservations.append(usage)

    async def send(images):
        with image_request_budget(images=(proof,), canonical_text_bytes=512, reserve=reserve):
            async with aclosing(adapter.stream_rich_messages(
                (ModelUserMessage(tuple(images)),), configuration, max_tokens=16,
            )) as stream:
                return [event async for event in stream]

    async def run():
        events = await send((block, block))
        assert any(isinstance(event, ModelStreamDone) for event in events)
        assert len(local_model.bodies) == 1
        body = local_model.bodies[0]
        payload = json.loads(body)
        assert len([part for part in payload["messages"][0]["content"] if part["type"] == "image_url"]) == 2
        assert len(local_model.reservations) == 1, "real serialized send did not reserve its images"
        usage = local_model.reservations[0]
        assert local_model.reservations_at_arrival == [1]
        assert usage.protocol == "openai-completions"
        assert usage.image_occurrences == 2
        assert usage.image_base64_bytes == len(data) * 2
        assert usage.image_input_estimate == 16384
        assert usage.body_bytes == len(body)
        assert usage.body_sha256 == hashlib.sha256(body).hexdigest()
        assert usage.wire_non_image_bytes == len(body) - len(data) * 2
        assert usage.envelope_bytes == max(0, len(body) - len(data) * 2 - 512)

        # Reusing the same image in a later payload spends again.
        await send((block,))
        assert [item.image_occurrences for item in local_model.reservations] == [2, 1]
        assert local_model.reservations_at_arrival == [1, 2]
        events = await send((block, block))
        assert any(isinstance(event, RichModelStreamError) for event in events)
        assert len(local_model.bodies) == 2, "exhausted image budget reached the provider"
        assert [item.image_occurrences for item in local_model.reservations] == [2, 1]

    asyncio.run(run())


PROTOCOLS = (
    "openai-completions", "openai-responses", "azure-openai-responses",
    "openai-codex-responses", "anthropic-messages", "google-generative",
    "google-vertex", "mistral-conversations", "bedrock-converse", "pi-messages",
)


def protocol_response(protocol, payload):
    stream = payload.get("stream") or protocol in {"openai-codex-responses", "pi-messages"}

    def sse(*events):
        return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()

    if protocol in {"openai-completions", "mistral-conversations"}:
        if stream:
            return sse({"choices": [{"index": 0, "delta": {"content": "ok"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}) + b"data: [DONE]\n\n"
        return json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}).encode()
    if protocol in {"openai-responses", "azure-openai-responses", "openai-codex-responses"}:
        response = {"id": "local", "status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}
        return sse({"type": "response.completed", "response": response}) if stream else json.dumps(response).encode()
    if protocol == "anthropic-messages":
        if stream:
            return sse({"type": "message_start", "message": {"id": "local", "model": "local"}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"})
        return json.dumps({"id": "local", "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}).encode()
    if protocol in {"google-generative", "google-vertex"}:
        response = {"candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}]}
        return sse(response) if ":streamGenerateContent" in payload.get("_fixture_path", "") else json.dumps(response).encode()
    if protocol == "bedrock-converse":
        if payload.get("_fixture_path", "").endswith("converse-stream"):
            output = b""
            for event in ({"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}}, {"messageStop": {"stopReason": "end_turn"}}):
                data = json.dumps(event).encode()
                header = struct.pack(">II", len(data) + 16, 0)
                frame = header + struct.pack(">I", zlib.crc32(header)) + data
                output += frame + struct.pack(">I", zlib.crc32(frame))
            return output
        return json.dumps({"output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}}, "stopReason": "end_turn"}).encode()
    if protocol == "pi-messages":
        return sse({"type": "text_start"}, {"type": "text_delta", "delta": "ok"}, {"type": "text_end"}, {"type": "done", "reason": "stop"})
    raise AssertionError("fixture protocol is not implemented")


def sent_image_data(protocol, body):
    """Independent literal protocol locations in the actual server payload."""
    if protocol in {"openai-completions", "mistral-conversations"}:
        return [part["image_url"]["url"].split(";base64,", 1)[1]
            for message in body["messages"] if isinstance(message.get("content"), list)
            for part in message["content"] if part["type"] == "image_url"]
    if protocol in {"openai-responses", "azure-openai-responses", "openai-codex-responses"}:
        return [part["image_url"].split(";base64,", 1)[1]
            for message in body["input"] if isinstance(message.get("content"), list)
            for part in message["content"] if part["type"] == "input_image"]
    if protocol == "anthropic-messages":
        return [part["source"]["data"] for message in body["messages"] for part in message["content"] if part["type"] == "image"]
    if protocol in {"google-generative", "google-vertex"}:
        return [part["inlineData"]["data"] for message in body["contents"] for part in message["parts"] if "inlineData" in part]
    if protocol == "bedrock-converse":
        return [part["image"]["source"]["bytes"] for message in body["messages"] for part in message["content"] if "image" in part]
    return [part["data"] for message in body["context"]["messages"] if isinstance(message.get("content"), list)
        for part in message["content"] if part["type"] == "image"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ("stream", "complete"))
def test_registered_protocol_reserves_exact_actual_image_payload(local_model, protocol, mode, record_property):
    from backend.app.ai.request_budget import ImageProof, image_request_budget

    data = fixture_image()
    image = ModelImageBlock("image/png", data)
    proof = ImageProof("image/png", hashlib.sha256(data.encode()).hexdigest(), len(data))
    configuration = ModelConfiguration(providerId="local", provider="local", api=protocol,
        baseUrl=local_model.url, model="pixtral-12b", apiKey="synthetic-test-key",
        apiOptions={"accountId": "synthetic-account", "project": "synthetic-project"})
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))
    local_model.response = lambda payload: protocol_response(protocol, payload)

    async def reserve(usage):
        local_model.reservations.append(usage)

    async def run():
        with image_request_budget(images=(proof,), canonical_text_bytes=512, reserve=reserve):
            messages = (ModelUserMessage((image, image)),)
            if mode == "complete":
                response = await adapter.complete_rich_messages(messages, configuration, max_tokens=16)
                assert response.content, "local completion did not finish"
            else:
                async with aclosing(adapter.stream_rich_messages(messages, configuration, max_tokens=16)) as stream:
                    events = [event async for event in stream]
                assert any(isinstance(event, ModelStreamDone) for event in events), "local stream did not finish"

    asyncio.run(run())
    assert len(local_model.bodies) == 1
    raw = local_model.bodies[0]
    actual_images = sent_image_data(protocol, json.loads(raw))
    assert len(actual_images) == 2 and all(hashlib.sha256(value.encode()).hexdigest() == proof.base64_sha256 for value in actual_images)
    record_property("actual_body_bytes", len(raw))
    record_property("actual_body_sha256", hashlib.sha256(raw).hexdigest())
    record_property("actual_image_occurrences", 2)
    record_property("reservations_at_arrival", local_model.reservations_at_arrival)
    assert len(local_model.reservations) == 1, "actual protocol send had no reservation"
    usage = local_model.reservations[0]
    assert local_model.reservations_at_arrival == [1]
    assert (usage.protocol, usage.image_occurrences, usage.image_base64_bytes, usage.image_input_estimate) == (protocol, 2, len(data) * 2, 16384)
    assert (usage.body_bytes, usage.body_sha256) == (len(raw), hashlib.sha256(raw).hexdigest())
    assert usage.wire_non_image_bytes == len(raw) - len(data) * 2


@pytest.mark.parametrize("case", ("unproved", "changed", "media", "text", "schema", "unicode_envelope", "image_sum"))
def test_unproved_or_over_budget_content_cannot_reach_the_provider(local_model, case):
    from backend.app.ai import ModelTextBlock
    from backend.app.ai.request_budget import ImageProof, image_request_budget

    data = fixture_image()
    canonical = 48 * 1024
    tools: tuple[dict[str, object], ...] = ()
    if case == "image_sum":
        output = io.BytesIO()
        Image.frombytes("RGB", (400, 330), random.Random(191).randbytes(400 * 330 * 3)).save(output, format="PNG")
        data = base64.b64encode(output.getvalue()).decode()
        assert 512 * 1024 < len(data) < 768 * 1024
    digest = hashlib.sha256(data.encode()).hexdigest()
    proof = ImageProof("image/png", "0" * 64 if case == "unproved" else digest, len(data))
    block = ModelImageBlock("image/jpeg" if case == "media" else "image/png", data + "!" if case == "changed" else data)
    messages = (ModelUserMessage((block, block) if case == "image_sum" else (block,)),)
    if case in {"text", "schema", "unicode_envelope"}:
        text = "雪" * 3000 if case == "unicode_envelope" else data * 14
        messages = (ModelUserMessage((ModelTextBlock(text if case != "schema" else "hello"),)),)
        if case == "schema":
            tools = ({"name": "local", "description": text, "parameters": {"type": "object", "properties": {}}},)
        canonical = 0 if case == "unicode_envelope" else canonical
    configuration = ModelConfiguration(providerId="local", provider="local", api="openai-completions",
        baseUrl=local_model.url, model="local-vision", apiKey="synthetic-test-key")
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))

    async def reserve(usage):
        local_model.reservations.append(usage)

    async def run():
        with image_request_budget(images=(proof,), canonical_text_bytes=canonical, reserve=reserve):
            async with aclosing(adapter.stream_rich_messages(messages, configuration, tools, max_tokens=16)) as stream:
                events = [event async for event in stream]
        assert any(isinstance(event, RichModelStreamError) for event in events)

    asyncio.run(run())
    assert local_model.bodies == [], "unproved or excess content reached a real socket"
    assert local_model.reservations == []


def test_cancellation_after_real_arrival_keeps_its_reservation(local_model):
    from backend.app.ai.request_budget import ImageProof, image_request_budget

    data = fixture_image()
    proof = ImageProof("image/png", hashlib.sha256(data.encode()).hexdigest(), len(data))
    configuration = ModelConfiguration(providerId="local", provider="local", api="openai-completions",
        baseUrl=local_model.url, model="local-vision", apiKey="synthetic-test-key")
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=3))
    release = threading.Event()

    def slow_response(payload):
        assert release.wait(3), "fixture did not release its owned response"
        return protocol_response("openai-completions", payload)

    local_model.response = slow_response

    async def reserve(usage):
        local_model.reservations.append(usage)

    async def send():
        with image_request_budget(images=(proof,), canonical_text_bytes=512, reserve=reserve):
            async with aclosing(adapter.stream_rich_messages(
                (ModelUserMessage((ModelImageBlock("image/png", data),)),), configuration, max_tokens=16,
            )) as stream:
                return [event async for event in stream]

    async def run():
        task = asyncio.create_task(send())
        try:
            end = asyncio.get_running_loop().time() + 2
            while not local_model.arrived.is_set():
                assert asyncio.get_running_loop().time() < end, "local request never arrived"
                await asyncio.sleep(.001)
            assert len(local_model.reservations) == 1
            task.cancel()  # The request owner's sole cancellation.
            with suppress(asyncio.CancelledError):
                await task
        finally:
            release.set()
            if not task.done():
                await task
        assert len(local_model.bodies) == 1
        assert len(local_model.reservations) == 1
        assert local_model.reservations[0].image_input_estimate == 8192

    asyncio.run(run())


def test_async_reservation_finishes_before_send_and_history_spends_again(local_model):
    from backend.app.ai import ModelTextBlock
    from backend.app.ai.request_budget import ImageProof, image_request_budget

    data = fixture_image()
    proof = ImageProof("image/png", hashlib.sha256(data.encode()).hexdigest(), len(data))
    configuration = ModelConfiguration(providerId="local", provider="local", api="openai-completions",
        baseUrl=local_model.url, model="local-vision", apiKey="synthetic-test-key")
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=3))
    original_user = ModelUserMessage((ModelImageBlock("image/png", data),))

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def reserve(usage):
            local_model.reservations.append(usage)
            entered.set()
            await release.wait()

        async def send(messages):
            with image_request_budget(images=(proof,), canonical_text_bytes=1024, reserve=reserve):
                async with aclosing(adapter.stream_rich_messages(messages, configuration, max_tokens=16)) as stream:
                    events = [event async for event in stream]
            return next(event.message for event in events if isinstance(event, ModelStreamDone))

        ready = asyncio.create_task(entered.wait())
        first = asyncio.create_task(send((original_user,)))
        try:
            done, _ = await asyncio.wait({ready, first}, timeout=2)
            assert ready in done and not first.done(), "provider send did not await the reservation"
            assert local_model.bodies == []
            assert not local_model.arrived.is_set()
        finally:
            release.set()
            entered.set()
            await ready
            response = await first
        # Actual user-image history remains in the second serialized request.
        await send((original_user, response, ModelUserMessage((ModelTextBlock("continue"),))))
        assert len(local_model.bodies) == 2
        assert local_model.reservations_at_arrival == [1, 2]
        assert [usage.image_occurrences for usage in local_model.reservations] == [1, 1]
        assert sum(usage.image_input_estimate for usage in local_model.reservations) == 16384
        for raw, usage in zip(local_model.bodies, local_model.reservations):
            assert len(sent_image_data("openai-completions", json.loads(raw))) == 1
            assert usage.body_sha256 == hashlib.sha256(raw).hexdigest()
            assert usage.image_base64_bytes == len(data)

    asyncio.run(run())


def test_text_only_request_in_image_scope_still_reserves_wire_envelope(local_model):
    from backend.app.ai import ModelTextBlock
    from backend.app.ai.request_budget import image_request_budget

    configuration = ModelConfiguration(providerId="local", provider="local", api="openai-completions",
        baseUrl=local_model.url, model="local-vision", apiKey="synthetic-test-key")
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))

    async def reserve(usage):
        local_model.reservations.append(usage)

    async def run():
        with image_request_budget(images=(), canonical_text_bytes=0, reserve=reserve):
            async with aclosing(adapter.stream_rich_messages(
                (ModelUserMessage((ModelTextBlock("bounded text only"),)),), configuration, max_tokens=16,
            )) as stream:
                events = [event async for event in stream]
        assert any(isinstance(event, ModelStreamDone) for event in events)

    asyncio.run(run())
    assert len(local_model.bodies) == 1
    assert local_model.reservations_at_arrival == [1]
    usage = local_model.reservations[0]
    assert (usage.image_occurrences, usage.image_base64_bytes, usage.image_input_estimate) == (0, 0, 0)
    assert usage.body_bytes == usage.wire_non_image_bytes == usage.envelope_bytes == len(local_model.bodies[0])
    assert usage.body_sha256 == hashlib.sha256(local_model.bodies[0]).hexdigest()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_calls_without_image_budget_keep_existing_error_details(local_model, protocol):
    from backend.app.ai import ModelTextBlock

    local_model.status = 500
    local_model.response = lambda payload: b'{"error":{"message":"local provider detail preserved"}}'
    configuration = ModelConfiguration(providerId="local", provider="local", api=protocol,
        baseUrl=local_model.url, model="pixtral-12b", apiKey="synthetic-test-key",
        apiOptions={"accountId": "synthetic-account", "project": "synthetic-project"})
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))

    async def run():
        async with aclosing(adapter.stream_rich_messages(
            (ModelUserMessage((ModelTextBlock("hello"),)),), configuration, max_tokens=16,
        )) as stream:
            errors = [event.error async for event in stream if isinstance(event, RichModelStreamError)]
        assert any("local provider detail preserved" in error for error in errors)

    asyncio.run(run())
    assert len(local_model.bodies) == 1
    assert local_model.reservations_at_arrival == [0]


def protocol_error_response(protocol, echo):
    if protocol == "bedrock-converse":
        data = json.dumps({"internalServerException": {"message": echo}}).encode()
        header = struct.pack(">II", len(data) + 16, 0)
        frame = header + struct.pack(">I", zlib.crc32(header)) + data
        return frame + struct.pack(">I", zlib.crc32(frame))
    error = {"type": "error", "error": {"message": echo}, "errorMessage": echo, "message": echo}
    return ("data: " + json.dumps(error) + "\n\n").encode()


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ("stream", "complete"))
@pytest.mark.parametrize("failure", ("http", "event"))
def test_image_reservation_survives_provider_errors_without_echo_leaks(local_model, protocol, mode, failure):
    from backend.app.ai.request_budget import ImageProof, image_request_budget

    data = fixture_image()
    proof = ImageProof("image/png", hashlib.sha256(data.encode()).hexdigest(), len(data))
    configuration = ModelConfiguration(providerId="local", provider="local", api=protocol,
        baseUrl=local_model.url, model="pixtral-12b", apiKey="synthetic-test-key",
        apiOptions={"accountId": "synthetic-account", "project": "synthetic-project"})
    adapter = RoutedModelAdapter(ModelAdapterRegistry(timeout=2))
    if failure == "http":
        local_model.status = 500
        local_model.response = lambda payload: json.dumps({"error": {"message": data}}).encode()
    else:
        local_model.response = lambda payload: protocol_error_response(protocol, data)

    async def reserve(usage):
        local_model.reservations.append(usage)

    async def run():
        errors = []
        with image_request_budget(images=(proof,), canonical_text_bytes=512, reserve=reserve):
            messages = (ModelUserMessage((ModelImageBlock("image/png", data),)),)
            if mode == "complete":
                try:
                    await adapter.complete_rich_messages(messages, configuration, max_tokens=16)
                except Exception as error:
                    errors.append(str(error))
            else:
                async with aclosing(adapter.stream_rich_messages(messages, configuration, max_tokens=16)) as stream:
                    errors = [event.error async for event in stream if isinstance(event, RichModelStreamError)]
        assert errors, "fixture did not produce a model error"
        leaked = any(data[:64] in error for error in errors)
        assert not leaked, "provider error exposed image data"

    asyncio.run(run())
    assert len(local_model.bodies) == 1, "provider error was implicitly retried"
    assert len(local_model.reservations) == 1
    assert local_model.reservations[0].image_occurrences == 1
    assert local_model.reservations_at_arrival == [1]
