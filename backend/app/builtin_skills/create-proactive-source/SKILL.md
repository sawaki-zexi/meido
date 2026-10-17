---
id: create-proactive-source
version: 1.0.0
description: 创建或更新主动 alert/content/context 来源，准备角色私有 MCP 源码、合成夹具及许可草稿，完成主人信任和真实后续消费。
tools: [tool_search, mcp_list, mcp_add, list_dir, read_file, write_file, edit_file]
activation: explicit
---
# 创建主动来源

这是作者流程，不授予命令执行、MCP、凭据或自主许可。先用当前可用的 tool_search/mcp_list 盘点当前角色获准服务，优先复用；缺 MCP 时说明在 MCP 设置选择固定 command/cwd、角色及凭据名称的路径。不要扫描全局配置、读取聊天密钥、修改其他角色或直接改受信服务。

新产物写到当前角色 workspace 的 `mcp/<name>/`。以下完整模板在本正文中；不需要读取分发目录的 assets。用 write_file 写六份文件并用 read_file 核对；文件工具关闭时准确说明缺项。模板只使用 Python 3.11+ 标准库，无安装命令、账号、网络、模型或 Shell 依赖。可先生成草稿；实际解释器须由主人在 MCP 设置选择现存绝对 Python 路径，PATH 不含 python 不等于已配置的绝对解释器不可用。

按需要选择 alert（事件）、content（内容和正文）、context（背景）；三种都由实际 A01 模型判断，不承诺 severity 直发或 HyDE。稳定 ID 在明确事件域内唯一；未 ACK 持续返回，失败必须 isError，不能用 [] 冒充空。context 不需 ACK。更新产物后旧 hash/proof 失效，须重新预览、信任和验证。

## 主人完成的步骤

1. 在角色自主设置填写相对清单路径 `mcp/<name>/manifest.json`，点击“预览来源产物”。宿主只读计算真实 hash，不执行或注册。复制“修正后的清单”，请本角色用 write_file 原样保存，再预览到 hash 一致。不要自己猜 SHA，也不要为此开放 Python/Shell。
2. 在 MCP 设置添加/明确更新服务：command 为 `[绝对Python, "-I", 绝对source.py]`，cwd 为该产物目录，明确勾选当前角色。环境秘密只通过现有凭据 UI 输入；源码和草稿只记录所需 key/ref，不写明文值。缺 env 或服务许可时保留草稿和具体配置说明。
3. 预览时选择实际保存的 serverId，复制当前 host 绑定的来源草稿到“来源许可草稿”；检查每项 get/poll/ACK、事件域和 enabled，显式保存。A01/Drift 仍默认关闭，不自动勾选。
4. 主人勾选 `trustLocalCode`，给实际 manifest SHA，通过原受管验证运行 `--meido-contract-test`。它使用独立无凭据 state；绝不自测真实账号 pull/ACK，不清理业务 ACK。失败/中断保留产物与真实错误，不声称可执行。
5. 验证通过后主人显式使用 proof 并保存；context 仅有读取契约证明，不授予 ACK 重试。另行选模型和有限自主许可后，下个合格 clock 才消费同一文件。验收须看到真实桌面决定/skip、ACK 回执及重启后抑制；写文件或验证通过不等于已开启或已收到真实服务数据。

## 六份文件

模板是可启动的离线合成来源。接入真实服务时只替换实际数据读取逻辑，保留错误、事件身份、隔离测试和持久 ACK；真实账号读取/ACK 需具体主人许可。不要执行任意生成脚本来绕过现有 Shell 规则。

### `catalog.json`

```json
[
  {"name":"get_alerts","description":"Read pending alert events","inputSchema":{"type":"object","properties":{},"additionalProperties":false}},
  {"name":"get_contents","description":"Read pending content with associated body","inputSchema":{"type":"object","properties":{},"additionalProperties":false}},
  {"name":"get_context","description":"Read background context","inputSchema":{"type":"object","properties":{},"additionalProperties":false}},
  {"name":"poll","description":"Refresh the local synthetic source","inputSchema":{"type":"object","properties":{},"additionalProperties":false}},
  {"name":"ack","description":"Persist the original absolute acknowledgement","inputSchema":{"type":"object","properties":{"event_ids":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":200},"ack_id":{"type":"string","minLength":1},"expires_at":{"type":["number","null"]},"ttl_hours":{"type":"integer","minimum":0}},"required":["event_ids","ack_id","expires_at","ttl_hours"],"additionalProperties":false}}
]
```

### `fixture.json`

```json
{"events":[{"id":"alert-demo-1","kind":"alert","title":"本地合成事件","summary":"不是实际账号通知"},{"id":"content-demo-1","kind":"content","title":"本地合成正文","body":"content-demo-1 的固定关联正文"},{"id":"context-demo-1","kind":"context","title":"本地背景","data":{"state":"synthetic"}}]}
```

### `source.py`

```python
"""Offline source; generated from Meido's MIT Shiori port. No account I/O."""
import json
import math
from pathlib import Path
import sqlite3
import sys
import time


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    test = len(sys.argv) == 3 and sys.argv[1] == "--meido-contract-test"
    if not test and len(sys.argv) != 1:
        raise ValueError("unsupported launch arguments")
    artifact = Path(__file__).resolve().parent
    root = Path(sys.argv[2]) if test else artifact / "state"
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "source.sqlite", timeout=1)
    db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS acks(ack_id TEXT PRIMARY KEY,receipt TEXT NOT NULL)")
    db.commit()

    def read(key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def store(key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))

    def now():
        return read("now", time.time()) if test else time.time()

    def call(name, args):
        if test and name == "meido_test_seed":
            with db:
                for key in ("events", "now", "mode", "event_domain"):
                    store(key, args.get(key, "ok" if key == "mode" else None))
                store("lost", int(bool(args.get("lose_first_ack_receipt"))))
            return {"seeded": True, "event_domain": args["event_domain"]}
        if test and name == "meido_test_clock":
            with db:
                store("now", args["now"])
            return {"now": args["now"]}
        if name in ("get_alerts", "get_contents", "get_context"):
            if test and read("mode") == "error":
                raise ValueError("synthetic read failure")
            events = read("events", []) if test else json.loads((artifact / "fixture.json").read_text(encoding="utf-8"))["events"]
            if not isinstance(events, list):
                raise ValueError("invalid event collection")
            kind = {"get_alerts": "alert", "get_contents": "content", "get_context": "context"}[name]
            suppressed = set()
            for row in db.execute("SELECT receipt FROM acks"):
                ack = json.loads(row[0])
                if ack["expires_at"] is None or ack["expires_at"] > now():
                    suppressed.update(ack["event_ids"])
            return [event for event in events if event["kind"] == kind and event["id"] not in suppressed]
        if name == "poll":
            return {"polled": True}
        if name == "ack":
            ids, identifier, expiry = args["event_ids"], args["ack_id"], args["expires_at"]
            if not ids or len(ids) != len(set(ids)) or not all(isinstance(item, str) and item for item in ids):
                raise ValueError("invalid event identity")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("missing acknowledgement identity")
            if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry)):
                raise ValueError("invalid absolute expiry")
            with db:
                old = db.execute("SELECT receipt FROM acks WHERE ack_id=?", (identifier,)).fetchone()
                if old:
                    receipt = json.loads(old[0])
                    if receipt["event_ids"] != ids:
                        raise ValueError("acknowledgement identity conflicts")
                else:
                    receipt = {"event_ids": ids, "ack_id": identifier, "expires_at": expiry}
                    db.execute("INSERT INTO acks VALUES(?,?)", (identifier, json.dumps(receipt)))
                lost = test and read("lost", 0)
                if lost:
                    store("lost", 0)
            if lost:
                raise ValueError("accepted but synthetic receipt unavailable")
            return receipt
        raise ValueError("unknown tool")

    try:
        for raw in sys.stdin.buffer:
            request = json.loads(raw)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"authored-source","version":"1"}}
            elif method == "tools/list":
                tools = json.loads((artifact / "catalog.json").read_text(encoding="utf-8"))
                if test:
                    tools += [{"name": name, "description":"isolated synthetic control", "inputSchema":{"type":"object","properties":{},"additionalProperties":True}} for name in ("meido_test_seed", "meido_test_clock")]
                result = {"tools": tools}
            elif method == "tools/call":
                try:
                    params = request["params"]
                    payload = call(params["name"], params.get("arguments", {}))
                    result = {"content":[{"type":"text","text":json.dumps(payload, ensure_ascii=False)}]}
                except (KeyError, ValueError, OSError, sqlite3.Error):
                    result = {"isError":True,"content":[{"type":"text","text":"source operation failed"}]}
            else:
                print(json.dumps({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32601,"message":"unknown method"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}, ensure_ascii=False), flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    main()
```

### `source-draft.json`

```json
{"serverId":"authored_source","eventDomain":"authored-source-v1","secretRefs":[],"sources":[{"sourceId":"authored_alert","channel":"alert","getTool":"get_alerts","ackTool":"ack"},{"sourceId":"authored_content","channel":"content","getTool":"get_contents","pollTool":"poll","ackTool":"ack"},{"sourceId":"authored_context","channel":"context","getTool":"get_context"}]}
```

### `manifest.json`

```json
{"profile":"meido-source-author-v2","version":2,"entrypoint":"source.py","files":{"source.py":"PREVIEW_REQUIRED","catalog.json":"PREVIEW_REQUIRED","fixture.json":"PREVIEW_REQUIRED","source-draft.json":"PREVIEW_REQUIRED","README.md":"PREVIEW_REQUIRED"}}
```

### `README.md`

```text
离线合成主动来源

依赖：Python 3.11+ 标准库（json/math/pathlib/sqlite3/sys/time）；无需第三方包或安装步骤。
启动：主人在MCP设置选择实际绝对Python路径，参数逐项为-I与本目录绝对source.py，cwd为本目录。
数据：正常模式只读取fixture.json并写本目录state/source.sqlite，无账号、网络或模型调用。
凭据：示范无需凭据；真实服务适配只能列所需key/ref名称，由主人通过现有MCP凭据UI填值，禁止写明文。
状态：当前是作者草稿。需只读hash预览并保存修正清单、主人MCP trust、具体来源许可和受管契约验证。
验证：--meido-contract-test使用宿主独立临时state，不读取/清理正常业务ACK；验证通过不自动启用。
启用：主人另行选择明确模型和有限自主许可，之后真实clock消费同一产物，查看原桌面receipt及ACK状态。
来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf create-proactive-source（MIT），按Meido契约改写。
```

新增执行依赖文件必须声明并重新预览；不要把凭据值写入任何产物。

来源：Shiori-Agent `4f266aeaf42fcd611362e2456417b91f659341cf`，`apps/backend/skills/create-proactive-source/SKILL.md`（MIT）。按 Meido 角色目录、显式许可、真实 stdio 和绝对期限持久 ACK 改写。
