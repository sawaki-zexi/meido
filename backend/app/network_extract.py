"""Bounded network document conversion worker (no network access or plugins).

HTML conversion options adapted from Shiori-Agent 4f266ae.
Copyright (c) 2026 YinFengWindy; see third_party/shiori-agent/LICENSE.
Run in a disposable process so cancellation/deadlines can stop CPU parsing.
"""
import json
import re
import sys

def search(request):
    text = request["text"]
    if request["sse"]:
        messages = []
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(line[5:].lstrip(" ") for line in event.splitlines() if line.startswith("data:"))
            if data and data != "[DONE]":
                messages.append(json.loads(data))
    else:
        messages = [json.loads(text)]
    mcp = request["mcp"]
    reply = next((message for message in messages if isinstance(message, dict) and (message.get("id") == 1 or not mcp)), None)
    if reply is None or "error" in reply:
        return {"error": "搜索服务协议错误或返回错误"}
    value = reply.get("result", {}) if mcp else reply
    if not isinstance(value, dict) or value.get("isError"):
        return {"error": "搜索服务返回错误"}
    content = value.get("content", []) if mcp else [{"type": "text", "text":
        f"{item.get('title', '')}\nURL: {item.get('url', '')}\n{item.get('text', '')}"}
        for item in value.get("results", []) if isinstance(item, dict)]
    if not isinstance(content, list):
        return {"error": "搜索服务协议错误"}
    combined = "\n\n".join(item["text"] for item in content if isinstance(item, dict)
        and item.get("type") == "text" and isinstance(item.get("text"), str)).strip()
    if not combined or combined.lower().rstrip(".!。") in {"no results", "no results found", "未找到结果", "没有结果"}:
        return {"error": "搜索结果为空"}
    sources = list(dict.fromkeys(match.group().rstrip(".,;") for match in re.finditer(r"https?://[^\s<>\]\)\"]+", combined)
        if len(match.group()) <= 4096))[:20]
    if not sources:
        return {"error": "搜索结果为空或未包含可核验来源"}
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
