"""Bounded network document conversion worker (no network access or plugins).

HTML conversion options adapted from Shiori-Agent 4f266ae.
Copyright (c) 2026 YinFengWindy; see third_party/shiori-agent/LICENSE.
Run in a disposable process so cancellation/deadlines can stop CPU parsing.
"""
import json
import re
import sys

# CR, LF and CRLF are all SSE line endings. Do not split one CRLF into
# two newlines through regex backtracking.
SSE_BOUNDARY = r"(?:\r\n|\r(?!\n)|(?<!\r)\n){2}"


def diagnostic(code, message, hint, *, retryable=False):
    """Only fixed, host-authored diagnostics may cross the tool boundary."""
    return {"error_code": code, "error": message, "hint": hint, "retryable": retryable}


def search(request):
    invalid = diagnostic("invalid_response", "搜索服务返回的协议内容无效",
                         "检查搜索服务端点与 provider 是否匹配；稍后重试或更换宿主搜索配置")
    try:
        text = request["text"].lstrip("\ufeff")
        if request["sse"]:
            messages = []
            for event in re.split(SSE_BOUNDARY, text):
                data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
                if data and data != "[DONE]":
                    messages.append(json.loads(data))
        else:
            messages = [json.loads(text)]
    except (ValueError, RecursionError):
        return invalid
    mcp = request["mcp"]
    reply = next((message for message in messages if isinstance(message, dict)
        and (not mcp or (message.get("jsonrpc") == "2.0" and type(message.get("id")) is int
                        and message["id"] == 1 and ("result" in message or "error" in message)))), None)
    if reply is None:
        return invalid
    value = reply.get("result") if mcp else reply
    if "error" in reply or isinstance(value, dict) and value.get("isError"):
        return diagnostic("provider_error", "搜索服务返回执行错误",
                          "稍后重试；若持续失败，请检查服务可用性、配额和宿主搜索配置", retryable=True)
    if not isinstance(value, dict):
        return invalid
    if mcp:
        content = value.get("content")
        if not isinstance(content, list) or any(not isinstance(item, dict)
                or item.get("type") != "text" or not isinstance(item.get("text"), str) for item in content):
            return invalid
    else:
        results = value.get("results")
        if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
            return invalid
        content = [{"text": f"{item.get('title', '')}\nURL: {item.get('url', '')}\n{item.get('text', '')}"}
                   for item in results]
    combined = "\n\n".join(item["text"] for item in content).strip()
    sources = list(dict.fromkeys(match.group().rstrip(".,;") for match in re.finditer(r'https?://[^\s<>\]\)\"]+', combined)
        if len(match.group()) <= 4096))[:20]
    if not combined or (not sources and re.match(
            r"^(?:no (?:search )?results(?: found)?|未找到结果|没有结果)(?:[.!。]|$)", combined, re.I)):
        return {"result": "", "results": [], "sources": [], "count": 0, "truncated": False,
                "hint": "未找到结果；可调整关键词后重新搜索"}
    if not sources:
        return diagnostic("missing_sources", "搜索结果未包含可核验来源",
                          "调整关键词后重试；不要把无来源的响应当作已核实事实")
    return {"result": combined[:request["limit"]], "sources": sources, "truncated": len(combined) > request["limit"]}


def convert(request):
    import html2text
    from lxml import html as lxml_html

    text = request["text"]
    if request["format"] != "html" and request["is_html"]:
        doc = lxml_html.fromstring(text, parser=lxml_html.HTMLParser(no_network=True, huge_tree=False))
        for element in doc.xpath("//script|//style|//noscript|//iframe|//object|//embed"):
            element.drop_tree()
        if request["format"] == "text":
            text = " ".join(" ".join(doc.itertext()).split())
        else:
            converter = html2text.HTML2Text()
            converter.body_width = 0
            converter.unicode_snob = True
            converter.protect_links = True
            text = converter.handle(lxml_html.tostring(doc, encoding="unicode")).strip()
    if not text.strip():
        raise ValueError("empty document")
    limit = request["limit"]
    return {"text": text[:limit], "truncated": len(text) > limit}


def main():
    request = json.loads(sys.stdin.buffer.read(32 * 1024 * 1024))
    result = search(request) if request.get("kind") == "search" else convert(request)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(1)
