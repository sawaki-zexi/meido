# 配置和验收 Feed Skill

状态：当前
最后核验：2026-10-16
事实来源：[Skill 契约](../reference/feed-skill.md)、[真实 HTTP 测试](../../tests/python/test_feed_skill_native.py)、[固定依赖记录](../research/feed-skill-native-acceptance-2026-10-16.md)。

## 准备独立服务

Feed 服务与 Meido 分开安装。协议匹配的验收候选为 [feed-mcp 固定提交](https://github.com/kachofugetsu09/feed-mcp/tree/210a79512c5cec008dd24c70f7c9c4e2616f5034)。先审阅 `run_mcp.py`、`src/mcp_bridge.py`、`src/feed_backend.py`，把 checkout 和数据放在 Meido 仓库之外。不要运行未知准备脚本或将服务源码复制进 Meido。

本轮 Windows 用 Python 3.12.10。原服务未锁 requirements，当前 mcp 2.x 已移除其使用的 FastMCP；不要直接安装未锁 requirements。使用独立 venv 和固定 wheel，例如：

```powershell
py -3.12 -m venv D:/tools/meido-feed/.venv
D:/tools/meido-feed/.venv/Scripts/python.exe -m pip install --only-binary=:all: mcp==1.30.0 requests==2.34.2 --report D:/tools/meido-feed/pip-report.json
D:/tools/meido-feed/.venv/Scripts/python.exe -m pip freeze --all
```

完整二级依赖与官方 wheel 哈希应随验收保存。Windows 上直接导入 Meido 的测试宿主还需项目已声明的 tzdata；本轮独立协议探针安装 `tzdata==2026.5`，不是 Feed 账号或付费凭据。

原服务固定读取 checkout 根目录的 `feed_mcp.json`；按其示例把 db_path 设为独立绝对数据路径。入口会切换到脚本目录并写 `feed_mcp.runtime.log`，不要把 cwd 当作数据隔离参数，也不要指向主人的现有订阅数据库。

## 主人授权与使用

在 MCP 设置中保存服务：ID 可填 `reading_room`，显示名称自定；command 为独立 Python 的绝对路径和 `run_mcp.py` 绝对路径，cwd 为实际 checkout。选择授权角色，保持“启用工具发现”。详见[现有 MCP 手册](mcp-services.md)。配置保存不会启动进程。

在该角色聊天输入 `$feed-manage 查看当前订阅`。角色从 mcp_list 中选定真实服务，连接后发现该服务实际返回的工具；多个候选时先选择目标。继续提供已确认的 RSS/Atom 地址和名称，添加后核对 list，再查询文章、概况、关键词或文章分页。

每次再问“现在订阅了什么”都应重新 list。取消前核对范围：原服务可能按名称子串/通配符批量删除并清历史。query 返回缓存不代表刚刷新成功，原服务也没有显式缓存过期标记。没有 grant、工具关闭、连接错误或撤权时，应先通过设置解决实际缺项。

env 字段按凭据保护：所有值都会从远端结果中清理。不要把 `PYTHONUTF8=1` 等不必要的短控制值填入服务 env；字符 `1` 也会被当作受保护值清理，影响文章 URL 展示。已有边界不会为 Feed 降低凭据保护。

## 复验

普通回归不需要真实服务，原生用例默认明确 skip：

```powershell
python -m pytest tests/python/test_feed_skill.py tests/python/test_feed_skill_native.py -q
```

显式启用后，缺源目录、Python、固定提交或依赖版本会失败，不会静默跳过：

```powershell
$env:MEIDO_FEED_NATIVE_TESTS = '1'
$env:MEIDO_FEED_SOURCE = 'D:/tools/feed-mcp'
$env:MEIDO_FEED_PYTHON = 'D:/tools/meido-feed/.venv/Scripts/python.exe'
$env:MEIDO_FEED_EVIDENCE_DIR = 'D:/test-artifacts/feed-http'
python -m pytest tests/python/test_feed_skill_native.py -q --basetemp D:/test-artifacts/feed-pytest
```

SOURCE 必须为固定 SHA、tracked 文件干净的外部 checkout。测试把审计过的运行文件复制到独立临时目录，配置临时 SQLite/HOME/日志，并通过真正 main HTTP、SkillRegistry、MCP 动态 schema 和脚本化模型边界读写 localhost RSS/Atom。不访问真实信息源、不调用付费模型或主动 poll/ACK。pytest 的 basetemp 应使用专用测试目录，避免覆盖既有证据。
