"""Real stdio peer for host integration tests; no network or paid services."""
import json
import base64
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

root = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"
root.mkdir(parents=True, exist_ok=True)
(root / "pid").write_text(str(os.getpid()))
lock = threading.Lock()
initialized = False


def send(value):
    with lock:
        print(json.dumps(value), flush=True)


def record(value):
    with lock, (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value) + "\n")


def handle(message):
    global initialized
    method = message.get("method")
    record({"method": method, "params": message.get("params", {})})
    if method == "notifications/initialized":
        initialized = True
        return
    if method == "notifications/cancelled":
        (root / "cancelled").write_text("yes")
        return
    request_id = message.get("id")
    if method == "initialize":
        if mode == "hang_init":
            return
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        if mode == "wrong_version":
            result["protocolVersion"] = "1999-01-01"
    elif not initialized:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "not initialized"}})
        return
    elif method == "tools/list":
        if mode == "malformed":
            print("this is not JSON", flush=True)
            return
        if mode == "oversize":
            print("x" * (5 * 1024 * 1024), flush=True)
            return
        schema = {"type": "object", "properties": {"text": {"type": "string", "pattern": "^evidence"}}, "required": ["text"], "additionalProperties": False}
        if mode == "remote_ref":
            schema = {"type": "object", "$ref": "https://forbidden.invalid/schema"}
        if mode == "bad_schema":
            schema = {"type": "object", "properties": {"text": {"type": "banana"}}}
        if mode == "deep_schema":
            schema = {"type": "object"}
            for _ in range(40):
                schema = {"type": "object", "properties": {"nested": schema}}
        if mode == "regex_input":
            schema = {"type": "object", "properties": {"text": {"type": "string", "pattern": "^(a+)+$"}}}
        if mode == "fanout_input":
            definitions = {"leaf": {"type": "object"}}
            previous = "leaf"
            for index in range(28):
                key = f"level{index}"
                definitions[key] = {"allOf": [{"$ref": f"#/$defs/{previous}"}, {"$ref": f"#/$defs/{previous}"}]}
                previous = key
            schema = {"type": "object", "$defs": definitions, "$ref": f"#/$defs/{previous}"}
        first = {"name": "echo", "description": "真实证据 echo", "inputSchema": schema, "annotations": {"readOnlyHint": True}}
        if mode == "long_name":
            first["name"] = "a" * 48
        if mode == "schema_valid":
            first["inputSchema"] = {"type": "object", "$defs": {"evidence": {"type": "string", "pattern": "^evidence"}}, "properties": {"text": {"$ref": "#/$defs/evidence"}}, "required": ["text"]}
            first["outputSchema"] = {"type": "object", "properties": {"evidence": {"type": "string"}}, "required": ["evidence"]}
        if mode == "regex_output":
            first["outputSchema"] = {"type": "object", "properties": {"evidence": {"type": "string", "pattern": "^(a+)+$"}}}
        if mode == "output_mismatch":
            first["outputSchema"] = {"type": "object", "properties": {"evidence": {"type": "integer"}}}
        if mode == "secret_declaration":
            first["description"] = os.environ.get("MCP_TEST_SECRET", "missing")
        second = {"name": "slow", "description": "等待后产生真实副作用", "inputSchema": {"type": "object"}}
        cursor = message.get("params", {}).get("cursor")
        result = {"tools": [first if mode == "duplicate" or not cursor else second]}
        if mode == "many_tools":
            result = {"tools": [dict(first, name=f"echo_{index}") for index in range(129)]}
        if mode == "large_catalog":
            result = {"tools": [dict(first, name=f"echo_{index}", description="x" * 8000) for index in range(80)]}
        if mode == "many_pages":
            index = int(cursor or "0")
            result = {"tools": [dict(first, name=f"echo_{index}")], "nextCursor": str(index + 1)}
        if (not cursor and mode != "many_pages") or mode == "cursor_loop":
            result["nextCursor"] = "page-2"
    elif method == "tools/call":
        name = message["params"]["name"]
        if name == "slow":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            (root / "child_pid").write_text(str(child.pid))
            (root / "started").write_text("yes")
            time.sleep(30)
            (root / "effect.txt").write_text("should be cancelled")
        value = message["params"].get("arguments", {}).get("text", "done")
        with (root / "business.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"name": name, "value": value}) + "\n")
        result = {"content": [{"type": "text", "text": value + " " + os.environ.get("MCP_TEST_SECRET", "missing")}], "structuredContent": {"evidence": value}}
        if mode == "secret_result":
            result["structuredContent"] = {"nested": [{"secret": os.environ["MCP_TEST_SECRET"]}]}
        if mode == "regex_output":
            result["structuredContent"] = {"evidence": "a" * 32 + "!"}
        if mode == "environment":
            result["structuredContent"] = {"keys": sorted(os.environ), "cwd": str(Path.cwd())}
        if mode == "large_result":
            result["content"] = [{"type": "text", "text": "bounded" * 10000}]
        if mode == "invalid_result":
            result["content"] = [{"type": "image", "mimeType": "image/png", "data": "not base64"}]
        if mode == "invalid_structured":
            result["structuredContent"] = ["not an object"]
        if mode == "secret_image":
            result["content"] = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(os.environ["MCP_TEST_SECRET"].encode()).decode()}]
        if mode == "tool_error":
            result["isError"] = True
        if mode == "peer_error":
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": os.environ["MCP_TEST_SECRET"]}})
            return
    else:
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "unknown method"}})
        return
    send({"jsonrpc": "2.0", "id": request_id, "result": result})
    if mode == "exit_after_list" and method == "tools/list" and message.get("params", {}).get("cursor"):
        threading.Timer(.1, lambda: os._exit(0)).start()
    if mode == "blocked_stdin" and method == "tools/list" and message.get("params", {}).get("cursor"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (root / "child_pid").write_text(str(child.pid))
        (root / "not_reading").write_text("yes")
        time.sleep(60)


for line in sys.stdin:
    message = json.loads(line)
    # Permit cancellation notifications while a business call is blocked.
    if message.get("method") == "tools/call":
        threading.Thread(target=handle, args=(message,), daemon=True).start()
    else:
        handle(message)
