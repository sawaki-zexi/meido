"""Bounded source snapshots use actual stdio and host-owned item identity."""
import asyncio
import json
import pytest

from backend.app.mcp_runtime import McpHost
from backend.app.proactive_runtime.config import SourceGrant
from backend.app.proactive_runtime.sources import collect_snapshot
from test_proactive_mcp import source

pytestmark = pytest.mark.usefixtures("local_platform_only")


def grant_for(source):
    _, _, _, frozen, _ = source
    return SourceGrant(sourceId="feed", channel="content", serverId=frozen.server_id,
        configRevision=frozen.config_revision, grantRevision=frozen.grant_revision,
        getTool="get_events", getDefinitionHash=frozen.operations[0][1],
        eventDomain="synthetic-events", enabled=True)


def snapshot(source):
    _, store, _, frozen, origin = source
    host = McpHost(store)

    async def exercise():
        bundle = host.freeze_autonomy(origin, (frozen,), authorized=lambda: True)
        try:
            return await collect_snapshot(bundle, [grant_for(source)], observed_at=1234,
                eligible=lambda *_: True, rotation=0)
        finally:
            await bundle.close()
            await host.close()
    return asyncio.run(exercise())


def test_real_source_body_and_ids_are_frozen_and_host_bound(source):
    root = source[0]
    events = [{"id": str(index), "kind": "content", "title": f"Item {index}", "body": "正文" * 5000}
              for index in range(7)]
    (root / "events.json").write_text(json.dumps(events), encoding="utf-8")
    value = snapshot(source)
    assert len(value["contents"]) == 5
    assert len({item["id"] for item in value["contents"]}) == 5
    assert value["contents"][0]["id"] != "0"
    assert value["contents"][0]["sourceId"] == "feed"
    assert all(len(body) == 8000 for body in value["bodies"].values())
    assert value["trustedEmpty"] is False
    assert value["sources"][0]["status"] == "ok"
    assert len((root / "calls.jsonl").read_text().splitlines()) == 1


def test_bad_source_payload_is_never_a_trusted_empty_snapshot(source):
    root = source[0]
    (root / "events.json").write_text('{"unexpected":"shape"}', encoding="utf-8")
    value = snapshot(source)
    assert value["sources"][0]["status"] == "failed"
    assert value["trustedEmpty"] is False
    assert value["contents"] == []


def test_denied_body_prefetch_retains_the_candidate_with_explicit_error(source):
    root, store, _, frozen, origin = source
    (root / "events.json").write_text(json.dumps([{"id": "url", "kind": "content", "url": "https://source.example/article"}]))
    host = McpHost(store)

    async def denied(_):
        raise PermissionError("the role did not grant autonomous webpages")

    async def exercise():
        bundle = host.freeze_autonomy(origin, (frozen,), authorized=lambda: True)
        try:
            value = await collect_snapshot(bundle, [grant_for(source)], observed_at=1234,
                eligible=lambda *_: True, rotation=0, fetch_body=denied)
            identifier = value["contents"][0]["id"]
            assert value["bodyErrors"][identifier]
            assert value["trustedEmpty"] is False
            assert len((root / "calls.jsonl").read_text().splitlines()) == 1
        finally:
            await host.close()
    asyncio.run(exercise())


def test_explicit_empty_is_distinct_from_failure_and_source_identity_override(source):
    root = source[0]
    (root / "events.json").write_text("[]", encoding="utf-8")
    empty = snapshot(source)
    assert empty["trustedEmpty"] is True
    assert empty["sources"][0]["status"] == "empty"
    (root / "events.json").write_text(json.dumps([
        {"id": "x", "kind": "content", "sourceId": "someone-else"}]), encoding="utf-8")
    rejected = snapshot(source)
    assert rejected["trustedEmpty"] is False
    assert rejected["sources"][0]["status"] == "failed"
