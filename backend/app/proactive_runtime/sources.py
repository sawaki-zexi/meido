"""Bounded static source snapshots; model reads never perform another poll."""
import asyncio
import hashlib
import json


_HOST_FIELDS = frozenset({"role_id", "roleId", "sourceId", "source_id", "ack_server", "_source", "serverId", "grantRevision"})


def _json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _decode(response):
    if response.get("isError") or _json_size(response) > 256 * 1024:
        raise ValueError("source response failed or exceeded its byte budget")
    if response.get("structuredContent") is not None:
        return response["structuredContent"]
    blocks = response.get("content")
    if (not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict)
            or blocks[0].get("type") != "text" or not isinstance(blocks[0].get("text"), str)):
        raise ValueError("source must return one JSON payload")
    def reject_constant(_):
        raise ValueError("source JSON has a non-finite value")
    return json.loads(blocks[0]["text"], parse_constant=reject_constant)


def _normalize(grant, payload, eligible):
    if grant.channel == "context" and isinstance(payload, dict):
        payload = [payload]
    elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
        payload = payload["events"]
    if not isinstance(payload, list):
        raise ValueError("source payload is not an event collection")
    incomplete = len(payload) > 200
    result, seen = [], set()
    for event in payload[:200]:
        if not isinstance(event, dict) or _HOST_FIELDS.intersection(event):
            raise ValueError("source cannot replace host identity")
        kind = event.get("kind", "context" if grant.channel == "context" else None)
        if kind not in {"alert", "content", "context"}:
            raise ValueError("source event kind is invalid")
        if kind != grant.channel:
            continue
        remote_id = event.get("event_id", event.get("id"))
        if event.get("event_id") is not None and event.get("id") is not None and event["event_id"] != event["id"]:
            raise ValueError("source event identity is ambiguous")
        if grant.channel == "context" and remote_id is None:
            remote_id = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        if not isinstance(remote_id, str) or not remote_id or len(remote_id) > 256 or remote_id in seen:
            raise ValueError("source event identity is missing or duplicated")
        seen.add(remote_id)
        if not eligible(grant, remote_id):
            continue
        identity = [grant.sourceId, grant.revision, grant.channel, remote_id]
        identifier = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        # Context may be an ordinary business dictionary without an event
        # envelope. Preserve those fields in the host-owned context projection.
        data = event.get("data", {key: value for key, value in event.items()
            if key not in {"kind", "id", "event_id", "title", "summary", "url", "body", "text"}}
            if grant.channel == "context" else {})
        if not isinstance(data, dict) or _json_size(data) > 16000:
            raise ValueError("source event data exceeds budget")
        values = {}
        for key in ("title", "summary", "url", "body", "text"):
            value = event.get(key, data.get(key, ""))
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise ValueError("source event text field has invalid type")
            values[key] = value
        item = {"id": identifier, "sourceId": grant.sourceId, "sourceRevision": grant.revision,
                "kind": grant.channel, "remoteId": remote_id, "eventDomain": grant.eventDomain,
                "title": values["title"][:2000], "summary": values["summary"][:4000],
                "url": values["url"][:8192], "data": data,
                "body": (values["body"] or values["text"])[:8000]}
        result.append(item)
    return result, incomplete


async def collect_snapshot(bundle, grants, *, observed_at, eligible, rotation, fetch_body=None):
    sources, alerts, content_groups, contexts = [], [], [], []
    for grant in grants:
        if not grant.enabled:
            continue
        state = {"sourceId": grant.sourceId, "revision": grant.revision,
                 "channel": grant.channel, "observedAt": observed_at, "truncated": False}
        try:
            response = await bundle.call(grant.serverId, grant.getTool, {})
            items, incomplete = _normalize(grant, _decode(response), eligible)
            state.update(status="incomplete" if incomplete else "ok" if items else "empty", truncated=incomplete)
            if grant.channel == "alert":
                alerts.extend(items)
            elif grant.channel == "content":
                content_groups.append(items)
            else:
                contexts.extend(items)
        except (ValueError, PermissionError, RuntimeError, TimeoutError):
            state.update(status="failed", error="source read or protocol validation failed")
        sources.append(state)
    # Interleave registered sources before advancing the persistent cursor.
    content = []
    while any(content_groups):
        for items in content_groups:
            if items:
                content.append(items.pop(0))
    if content:
        start = rotation % len(content)
        content = [*content[start:], *content[:start]][:5]
    alerts = alerts[:20]
    context_bytes = 0
    bounded_context = []
    for item in contexts:
        size = _json_size(item)
        if context_bytes + size > 32 * 1024:
            for state in sources:
                if state["sourceId"] == item["sourceId"]:
                    state.update(status="incomplete", truncated=True)
            continue
        context_bytes += size
        bounded_context.append(item)
    bodies, body_errors = {}, {}
    fetch_gate = asyncio.Semaphore(2)

    async def body(item):
        text = item.pop("body")
        if not text and item["url"] and fetch_body is not None:
            try:
                async with fetch_gate, asyncio.timeout(30):
                    text = await fetch_body(item["url"])
                if not isinstance(text, str):
                    raise ValueError("body fetch did not return text")
            except (ValueError, RuntimeError, TimeoutError, OSError):
                text = ""
                body_errors[item["id"]] = "body prefetch failed"
        bodies[item["id"]] = text[:8000]
        if not text:
            body_errors.setdefault(item["id"], "body is unavailable")
    await asyncio.gather(*(body(item) for item in content))
    for item in alerts:
        item.pop("body", None)
    value: dict[str, object] = {"sources": sources, "alerts": alerts, "contents": content,
             "contexts": bounded_context, "bodies": bodies, "bodyErrors": body_errors}
    while _json_size(value) > 512 * 1024 and (alerts or content or bounded_context):
        target = bounded_context if bounded_context else alerts if alerts else content
        removed = target.pop()
        bodies.pop(removed["id"], None)
        body_errors.pop(removed["id"], None)
        for state in sources:
            if state["sourceId"] == removed["sourceId"]:
                state.update(status="incomplete", truncated=True)
    value["trustedEmpty"] = (not alerts and not content
        and all(state["status"] in {"ok", "empty"} for state in sources))
    return value
