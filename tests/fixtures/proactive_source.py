"""Synthetic durable source over real MCP stdio, without account or network I/O."""
import json
import os
from pathlib import Path
import sys


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    contract = len(sys.argv) == 3 and sys.argv[1] == "--meido-contract-test"
    root = Path(sys.argv[2] if contract else sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    catalog = Path(__file__).parent / "catalog.json" if contract else root / "catalog.json"
    (root / "pid").write_text(str(os.getpid()))
    for line in sys.stdin.buffer:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "synthetic-source", "version": "1"}}
        elif method == "tools/list":
            tools = json.loads(catalog.read_text(encoding="utf-8"))
            if contract:
                tools += [{"name": name, "description": "isolated synthetic contract control",
                    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True}}
                    for name in ("meido_test_seed", "meido_test_clock")]
            result = {"tools": tools}
        elif method == "tools/call":
            params = request["params"]
            with (root / "calls.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(params, ensure_ascii=False) + "\n")
            if contract and params["name"] == "meido_test_seed":
                (root / "events.json").write_text(json.dumps(params["arguments"]["events"]), encoding="utf-8")
                (root / "clock").write_text(str(params["arguments"]["now"]))
                if params["arguments"].get("lose_first_ack_receipt"):
                    (root / "fail-ack-count").write_text("1")
                payload = {"seeded": True, "event_domain": params["arguments"]["event_domain"]}
            elif contract and params["name"] == "meido_test_clock":
                (root / "clock").write_text(str(params["arguments"]["now"]))
                payload = {"now": params["arguments"]["now"]}
            elif params["name"] == "get_events":
                payload = json.loads((root / "events.json").read_text(encoding="utf-8"))
                if contract:
                    saved = root / "acks.json"
                    acks = json.loads(saved.read_text()) if saved.exists() else {}
                    now = float((root / "clock").read_text())
                    suppressed = {identifier for ack in acks.values()
                        if ack.get("expires_at") is None or ack["expires_at"] > now
                        for identifier in ack["event_ids"]}
                    payload = [item for item in payload if item["id"] not in suppressed]
            elif params["name"] == "poll":
                payload = {"polled": True}
            else:
                acknowledgements = root / "acks.json"
                old = json.loads(acknowledgements.read_text()) if acknowledgements.exists() else {}
                identity = params["arguments"].get("ack_id", json.dumps(params["arguments"]["event_ids"]))
                old.setdefault(identity, params["arguments"])
                acknowledgements.write_text(json.dumps(old), encoding="utf-8")
                payload = old[identity]
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
            failure = root / "fail-ack-count"
            if params["name"] == "ack" and failure.exists() and int(failure.read_text()) > 0:
                failure.write_text(str(int(failure.read_text()) - 1))
                result = {"isError": True, "content": [{"type": "text", "text": "accepted but receipt unavailable"}]}
        else:
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
