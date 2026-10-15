import asyncio
import json

import httpx
import pytest

from backend.app.agent_runtime import CancellationToken, ToolContext
from backend.app.network_tools import NetworkToolsConfig, WebFetchTool, WebSearchTool


def context(token=None):
    return ToolContext("role-one", "role:role-one", "run-one", token or CancellationToken())


async def public_dns(host, port):
    return ["93.184.215.14"]


def test_role_fetches_text_with_verified_source_and_status():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="真实网页内容", headers={"content-type": "text/plain; charset=utf-8"})

    tool = WebFetchTool(NetworkToolsConfig(), resolver=public_dns, transport=httpx.MockTransport(handler))
    result = asyncio.run(tool.execute({"url": "https://example.org/article", "format": "text"}, context()))

    assert result.is_error is False
    data = json.loads(result.content)
    assert data["text"] == "真实网页内容"
    assert data["final_url"] == "https://example.org/article"
    assert data["status"] == 200
    assert requests[0].url.host == "93.184.215.14"
    assert requests[0].headers["host"] == "example.org"
    assert requests[0].extensions["sni_hostname"] == "example.org"


@pytest.mark.parametrize("format,expected", [
    ("text", "文章正文 阅读来源"),
    ("markdown", "[阅读来源](<https://example.org/source>)"),
    ("html", "<script>secret()</script>"),
])
def test_role_can_choose_html_markdown_or_clean_text(format, expected):
    html = '<html><head><style>.hidden{}</style></head><body><h1>文章正文</h1><a href="https://example.org/source">阅读来源</a><script>secret()</script></body></html>'
    tool = WebFetchTool(resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})))
    result = asyncio.run(tool.execute({"url": "https://example.org", "format": format}, context()))
    assert not result.is_error
    assert expected in result.details["text"]
    if format != "html":
        assert "secret" not in result.details["text"]


@pytest.mark.parametrize("target", ["http://localhost/", "http://127.0.0.1/", "http://[::1]/", "http://internal.example/", "http://mixed.example/", "http://224.0.0.1/", "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::7f00:1]/", "file:///etc/passwd", "http://user:password@example.org/", "http://router.local/", "http://localhost./"])
def test_fetch_denies_local_hosts_dns_answers_and_redirect_targets(target):
    requests = []

    async def dns(host, port):
        return {"internal.example": ["10.0.0.3"], "mixed.example": ["93.184.215.14", "192.168.0.1"]}.get(host, ["93.184.215.14"])

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": target})

    tool = WebFetchTool(resolver=dns, transport=httpx.MockTransport(handler))
    result = asyncio.run(tool.execute({"url": "https://example.org/start"}, context()))
    assert result.is_error
    assert "禁止" in result.content or "HTTP/HTTPS" in result.content
    assert len(requests) == 1


def test_fetch_follows_safe_redirect_with_final_source_and_rechecks_dns():
    queries = []

    async def dns(host, port):
        queries.append(host)
        return ["93.184.215.14"]

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://article.example/final"})
        return httpx.Response(200, text="final article", headers={"content-type": "text/plain"})

    result = asyncio.run(WebFetchTool(resolver=dns, transport=httpx.MockTransport(handler)).execute({"url": "https://example.org/start"}, context()))
    assert not result.is_error
    assert result.details["final_url"] == "https://article.example/final"
    assert queries == ["example.org", "article.example"]


def test_fetch_stops_download_at_byte_budget_without_consuming_rest():
    consumed = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in [b"a" * 32, b"b" * 32, b"never read"]:
                consumed.append(chunk)
                yield chunk

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Body(), headers={"content-type": "text/plain"}))
    result = asyncio.run(WebFetchTool(NetworkToolsConfig(max_bytes=40), resolver=public_dns, transport=transport).execute({"url": "https://example.org"}, context()))
    assert result.is_error
    assert "过大" in result.content
    assert len(consumed) == 2


def test_fetch_limits_returned_text_and_explains_no_pagination():
    tool = WebFetchTool(NetworkToolsConfig(max_chars=20), resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="x" * 30, headers={"content-type": "text/plain"})))
    result = asyncio.run(tool.execute({"url": "https://example.org"}, context()))
    assert not result.is_error
    assert result.details["text"] == "x" * 20
    assert result.details["truncated"] is True
    assert "分页" in result.details["note"]


@pytest.mark.parametrize("phase", ["dns", "body", "cancel"])
def test_network_deadline_and_cancellation_cover_dns_and_streams(phase):
    token = CancellationToken()

    async def dns(host, port):
        if phase == "dns":
            await asyncio.sleep(1)
        return ["93.184.215.14"]

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b"late body"

    async def run():
        tool = WebFetchTool(NetworkToolsConfig(timeout_seconds=.05), resolver=dns, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Body(), headers={"content-type": "text/plain"})))
        pending = asyncio.create_task(tool.execute({"url": "https://example.org"}, context(token)))
        if phase == "cancel":
            token.cancel()
        return await pending

    result = asyncio.run(run())
    assert result.is_error
    assert result.details["status"] == ("cancelled" if phase == "cancel" else "timed_out")


def test_html_conversion_is_included_in_deadline_without_blocking_event_loop():
    async def run():
        heartbeat = asyncio.Event()

        async def tick():
            await asyncio.sleep(.01)
            heartbeat.set()

        html = "<p><strong>nested content</strong></p>" * 50_000
        tool = WebFetchTool(NetworkToolsConfig(timeout_seconds=.05), resolver=public_dns, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html"})))
        timer = asyncio.create_task(tick())
        result = await tool.execute({"url": "https://example.org"}, context())
        await timer
        return result, heartbeat.is_set()

    result, responsive = asyncio.run(run())
    assert result.is_error
    assert result.details["status"] == "timed_out"
    assert responsive


@pytest.mark.parametrize("status,headers,body,expected", [
    (404, {"content-type": "text/html"}, b"not found", "HTTP 404"),
    (200, {"content-type": "image/png"}, b"PNG image", "二进制"),
    (200, {"content-type": "application/octet-stream"}, b"opaque", "二进制"),
    (200, {"content-type": "application/pdf"}, b"%PDF", "二进制"),
    (200, {"content-type": "text/plain"}, b"\x00\x01", "二进制"),
    (200, {"content-type": "text/html"}, b"", "为空"),
    (200, {"content-type": "text/plain", "content-length": "99999999"}, b"small", "过大"),
])
def test_network_errors_and_nontext_responses_do_not_masquerade_as_success(status, headers, body, expected):
    tool = WebFetchTool(resolver=public_dns, transport=httpx.MockTransport(lambda request: httpx.Response(status, content=body, headers=headers)))
    result = asyncio.run(tool.execute({"url": "https://example.org"}, context()))
    assert result.is_error
    assert expected in result.details["error"]


@pytest.mark.parametrize("sse", [True, False])
def test_role_search_preserves_shiori_options_and_parses_all_results(sse):
    requests = []

    def handler(request):
        requests.append(request)
        data = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": "Title: 第一条\nURL: https://example.org/one\n摘要一"},
            {"type": "text", "text": "Title: 第二条\nURL: https://example.org/two\n摘要二"},
        ]}}
        if sse:
            return httpx.Response(200, text="event: message\ndata: " + json.dumps(data) + "\n\n", headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=data)

    tool = WebSearchTool(resolver=public_dns, transport=httpx.MockTransport(handler))
    result = asyncio.run(tool.execute({"query": "今日发布", "num_results": 2, "livecrawl": "preferred", "type": "deep"}, context()))
    assert not result.is_error
    assert "摘要一" in result.details["result"] and "摘要二" in result.details["result"]
    assert result.details["sources"] == ["https://example.org/one", "https://example.org/two"]
    assert json.loads(requests[0].content)["params"] == {"name": "web_search_exa", "arguments": {
        "query": "今日发布", "numResults": 2, "livecrawl": "preferred", "type": "deep"}}


def test_host_can_select_authenticated_search_provider_without_exposing_credentials():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"title": "News", "url": "https://example.org/news", "text": "source article"}]})

    config = NetworkToolsConfig(search_provider="exa_api", search_endpoint="https://search.example/search",
        search_headers={"x-api-key": "secret-key"}, search_parameters={"category": "news"})
    tool = WebSearchTool(config, resolver=public_dns, transport=httpx.MockTransport(handler))
    result = asyncio.run(tool.execute({"query": "today"}, context()))
    assert not result.is_error
    assert result.details["sources"] == ["https://example.org/news"]
    assert requests[0].headers["x-api-key"] == "secret-key"
    assert json.loads(requests[0].content)["category"] == "news"
    assert json.loads(requests[0].content)["query"] == "today"
    assert "secret-key" not in result.content
    assert "secret-key" not in repr(config)


@pytest.mark.parametrize("reply", [
    {"jsonrpc": "2.0", "id": 1, "error": {"message": "secret-key"}},
    {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "secret-key"}]}},
    {"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
    {"jsonrpc": "2.0", "id": 1, "result": {"content": None}},
    {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "No results found"}]}},
    {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "No results found. Please try again."}]}},
])
def test_search_provider_errors_invalid_payload_and_empty_results_are_explicit(reply):
    result = asyncio.run(WebSearchTool(resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=reply))).execute({"query": "q"}, context()))
    assert result.is_error
    assert "secret-key" not in result.content


@pytest.mark.parametrize("tool_type,arguments", [
    (WebFetchTool, {"url": "https://example.org", "headers": {"authorization": "forged"}}),
    (WebFetchTool, {"url": "https://example.org", "role_id": "other-role"}),
    (WebSearchTool, {"query": "q", "endpoint": "https://evil.example"}),
    (WebSearchTool, {"query": "q", "session_key": "role:other-role"}),
])
def test_network_tools_reject_forged_identity_or_host_parameters_without_io(tool_type, arguments):
    def unexpected(request):
        raise AssertionError("forged parameters must not reach network")

    result = asyncio.run(tool_type(resolver=public_dns, transport=httpx.MockTransport(unexpected)).execute(arguments, context()))
    assert result.is_error


def test_search_accepts_completed_sse_result_without_waiting_for_connection_close():
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": keepalive\n\n"
            yield b'data: {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"Article https://example.org/one"}]}}\n\n'
            await asyncio.sleep(10)

    tool = WebSearchTool(NetworkToolsConfig(timeout_seconds=3), resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Events(), headers={"content-type": "text/event-stream"})))
    result = asyncio.run(tool.execute({"query": "article"}, context()))
    assert not result.is_error
    assert result.details["sources"] == ["https://example.org/one"]


def test_search_document_parsing_obeys_whole_operation_deadline():
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": "x" * 3_000_000 + " https://example.org/source"}]}}
    tool = WebSearchTool(NetworkToolsConfig(timeout_seconds=.001), resolver=public_dns,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    result = asyncio.run(tool.execute({"query": "q"}, context()))
    assert result.is_error
    assert result.details["status"] == "timed_out"


@pytest.mark.parametrize("size,expected_error", [(32, False), (2000, True)])
def test_compressed_response_is_streamed_with_decompressed_byte_budget(size, expected_error):
    import gzip

    class Compressed(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(b"a" * size)

    tool = WebFetchTool(NetworkToolsConfig(max_bytes=80), resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Compressed(), headers={"content-type": "text/plain", "content-encoding": "gzip"})))
    result = asyncio.run(tool.execute({"url": "https://example.org"}, context()))
    assert result.is_error == expected_error
    if not expected_error:
        assert result.details["text"] == "a" * 32


@pytest.mark.parametrize("encoding", ["gb18030", "utf-16", "utf-8-sig"])
def test_fetch_decodes_declared_text_charset_and_bom(encoding):
    body = "中文正文 hello".encode(encoding)
    result = asyncio.run(WebFetchTool(resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, headers={"content-type": f"text/plain; charset={encoding}"})))
        .execute({"url": "https://example.org"}, context()))
    assert not result.is_error
    assert result.details["text"] == "中文正文 hello"


def test_search_redacts_configured_credentials_even_if_provider_echoes_them():
    config = NetworkToolsConfig(search_headers={"Authorization": "Bearer host-secret"})
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
        {"type": "text", "text": "Debug echoed host-secret. Source https://example.org/article"}]}}
    result = asyncio.run(WebSearchTool(config, resolver=public_dns, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload))).execute({"query": "article"}, context()))
    assert not result.is_error
    assert "host-secret" not in result.content


def test_fetch_negotiates_requested_format_and_recognizes_case_insensitive_html():
    def handler(request):
        assert "text/markdown;q=1.0" in request.headers["accept"]
        assert "zh-CN" in request.headers["accept-language"]
        return httpx.Response(200, text="<h1>Article</h1>", headers={"content-type": "Text/HTML; charset=UTF-8"})

    result = asyncio.run(WebFetchTool(resolver=public_dns, transport=httpx.MockTransport(handler))
        .execute({"url": "https://example.org", "format": "markdown"}, context()))
    assert not result.is_error
    assert result.details["text"] == "# Article"
