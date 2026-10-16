# 断连监听取消修复的本地验收（#192）

状态：当前，本地窄验收和固定提交双轴审查完成；准确集成头df10901的两套完整CI均通过。

事实来源：[Issue #192](https://github.com/sawaki-zexi/meido/issues/192)、[监听实现](../../backend/app/main.py)、[HTTP 记忆回归](../../tests/python/test_memory_tools_api.py)、[完成回复回归](../../tests/python/test_api.py)、[停止对话回归](../../tests/python/test_chat_stop.py)。最后核验：2026-10-17。

## 已提取的行为

真实 Starlette Request 的非阻塞 AnyIO probe 与一次外部任务取消重叠时，监听任务的取消计数由 1 增至 2，probe 返回后仍为 1。旧监听只检查停止标志，因此会继续下一次 sleep。静态提取原监听 body 的最小探针在四个独立进程中均复现；这只是定位证据，不代替产品验收。

新增回归通过真实 `POST /api/roles/{id}/messages`、HTTPX ASGI transport、Request 和 AnyIO，在 receive 边界捕获当前 actor，不依赖内部函数名。父 HTTP 请求和受控 embedding 尚存活时，测试仅对该 actor 取消一次，先保存它是否退出，再取消客户端并释放测试持有的连接与 embedding。旧生产代码在最后的 actor-survived 断言失败；心跳、真实记忆记录 ID、MEMORY.md 回滚检查均先通过，进程自然结束且 guard 无尝试。

修复只让监听在每轮原有 sleep/probe 前检查自身尚未处理的取消请求并自然返回，新增五行；没有新增 `cancel` / `uncancel`，没有改变记忆超时、回滚、资源 owner 顺序或权限。相同产品回归随后通过，actor 在客户端清理前完成退出。此回归不声称客户端取消与 probe 在同一瞬间发生；原请求取消用例仍单独复验。

## 实际验收结果

| 边界 | 既有 WSL/Linux | 现装 Windows |
| --- | --- | --- |
| 新真实 HTTP actor 回归 | 旧代码 1 failed；修复后 1 passed | 修复后通过 |
| 原记忆 deadline / cancel_request 两参数 | 2 passed，原断言不变 | 2 passed |
| 记忆权限、写入退场、recall | 18 项逐例无失败或错误 | 本轮未扩测 |
| 完成回复与监听取消重叠 | 局部隔离依赖后 1 passed | 1 passed |
| 原停止对话 | 3 passed | 3 passed |
| 三个变更 Python 文件静态编译 | Python 3.12.7 通过 | Python 3.13.9 通过 |

Windows 的七项在一个隔离进程内完成（15.71 秒）。随后仅补新回归的任务类型声明和 callback 非空前提；受影响的新回归分别再次得到 Linux 1 passed / Windows 1 passed。生产源及原有六项测试内容未变，未仪式性重跑整套测试。

Linux 的首次 19 项组合实际为 **19 个 test body 通过、1 个 teardown ERROR**，不能记为整组通过：完成回复测试未隔离默认记忆依赖，inner guard 拦下 `dashscope.aliyuncs.com`。其余 18 项 JUnit 无失败/错误。只对原完成回复测试绑定 fresh `MemorySettingsStore(enabled=False)` 并显式使用 `local_platform_only` 后单独重验，body 和 teardown 均通过；共享 helper、业务断言和生产代码未因此改变。这是测试依赖隔离，不是产品修复。原 lookup 的现场 caller 未捕获，不能用静态队列路径或其他运行的 caller 冒充它。

各成功运行均使用 app/collection 前创建的全新 DATA、ROLES、OWNER、BROWSER、PROJECT、HOME、TEMP，清空继承环境和凭据入口；OS vault get 固定为空、set 拒绝、keyring import 拒绝。内外 localhost guard 持有至原线程及解释器终态。所有成功运行均自然退出、无 guard 尝试或 shutdown stall，最终仅 MainThread。

原 `test_chat_stop` fixture 保持不变：uvicorn `lifespan="off"`、记忆关闭，server thread join 并断言停止后，既有 `_close_memory_workers` 在新 loop 执行。本轮未观察到真实待清跨 loop owner；不能把该既有 close 描述成 server 原 loop 收尾。

## 静态检查与固定源码

仓库目前没有 Python 类型门禁；CI 使用 compileall 和 pytest。额外用现装 mypy 2.1.0 对精确 `f4ac973a04cdcd8327f80f20bd58d5088fdf8b77` 全树基线与修复的相同三个文件、相同参数作对照：目标 Python 3.12、`--follow-imports=skip --ignore-missing-imports --no-site-packages --check-untyped-defs --no-incremental`。两边均退出 1，保留 **27 项既有错误**；按未改行映射、列和消息逐项匹配，新增 0，不能称完整类型检查通过。原始 30 项结果及修正测试类型后的 27 项结果均保留。

| 最终执行工作树原始字节（CRLF） | SHA256 |
| --- | --- |
| backend/app/main.py | `395de4fcb261b406658199b06b2cc365d4a830391ec8a7327fa7e0e887fc4a08` |
| tests/python/test_memory_tools_api.py | `33252d1bd8bffbf30537d18df8daeb242d201d4054fe7ccff6549ccab5638afb` |
| tests/python/test_api.py | `cd70182002a7f2532367a3429c097afc009ebabedf3e3f47d98765575c64651f` |

Git 提交保存规范化 LF；下面是相同三个文件的 Git blob 指纹。已确认与执行工作树去除 CRLF 差异后的文本逐字一致，不把换行规范化当作行为变化。原执行工作树的记忆测试 12,605 字节前缀完全保留。

| Git 提交内容（LF） | SHA256 |
| --- | --- |
| backend/app/main.py | `93b886febb18fbe8d7a1d6758dffcc5147ee3dcec1ab5117f547392ad5080b1b` |
| tests/python/test_memory_tools_api.py | `d2e44afe1322dcfa74cbb44bacc4d413f6494bd17483d6f4687015dfaf7f9881` |
| tests/python/test_api.py | `1ae7d962b696e0c552e563bcfbd73eaeb5aded244d39f01c4dc43b32849dc904` |

每次执行前、pytest 返回后和权威进程终态后重新枚举全部 backend `.py` / `.json` 与 tests/python `.py`；192 独立树每次 474 项均无运行中改动。编译产物只写外部临时目录。

## 外部证据归档

以下为工作站 `memory-exit-diagnostic-7b008cb-v1` 归档中的完整原件索引，不随源码提交复制日志或合成凭据。

| 索引 | SHA256 |
| --- | --- |
| http-actor192-first-red-terminal-v1/terminal-index.json | `d113158dd68edc9f4a84816a1de5bdc54ebdecd3680b70ce25bc37f82fdfff0f` |
| http-actor192-first-green-terminal-v1/terminal-index.json | `705eb350a9c9fb16804f3db8afc7fa3859524c371f10a06221ea8f04ea456f4a` |
| windows-http192-terminal-v1/terminal-index.json | `1387b3333381a1cefa64f410779e417ecd37bce176cafbcae02a169f2f6d1547` |
| final-actor192-linux-terminal-v1/terminal-index.json | `06b9505982c5506ae1d7dfe373a5015025ff1a92c2f7df05c79fdc6d4355162e` |
| final-actor192-windows-terminal-v1/terminal-index.json | `f9a96bfad049063bc04621b09872ee35f6de70c8364a1f6193175e55dbd24972` |
| typing192-line-mapped-delta-v1.json | `c60ee19c8025cb255ed86e693cb239bdfc1ae6e85eff18bbc6808daef012b54f` |
| retained-intentionally-interrupted-terminal-v1/terminal-index.json | `be7c2acc1139edd8b2a6875f781bea64fcbc40abb19c8d14b413176c53d3bdc7` |

## 限制与尚未完成

- Linux 是既有 Kali / Python 3.12.7 / pytest 8.4.2 / Starlette 1.8.0 / AnyIO 4.15.1；已装 lark-oapi 1.7.5 与仓库固定 1.7.4 不同。Windows 是 Python 3.13.9 / pytest 9.1.1 / Starlette 1.1.0 / AnyIO 4.13.0；pytest 9.1.1 超出仓库 CI `<9`。不能称本地环境与 CI 等价。
- 旧原诊断 exec32765 的 deadline 曾通过，cancel_request 没有完整 verdict。按另行公开的专属收尾计划核 argv、starttime、fd、无子进程后，只向原 PID 的 pidfd 发送一次 SIGTERM；原工具退出码实际为 1，记录为有意中断，不是自然 app cleanup、guard 恢复或测试成功。没有最终 after.json / JUnit。所有旧原件保留，未重启旧诊断。
- 旧 CI 栈没有监听 task 身份；本次同类症状的 RED/GREEN 不能证明所有旧 CI 挂起完全同因。旧原诊断拦截的 Popen caller 仍未知。
- 上述执行记录固定独立源 `e6b9dd82cb3dabaedfd947af20042727557b4134`。其规范、需求规格两轴独立审查均为 0 findings，随后无冲突汇入总分支 `c36e7f396634061b463e5de87fcbc526780aacee`；三个 Python 文件及原验收文档的 Git blob 与受审源逐项相同。后续仅补本文集成状态，不重复本地验收。
- 集成核验索引 `D:/meido-artifacts/disconnect192-integration-root-verification-v1.json` 的 SHA256 为 `e403438f0426393d0d7c9c57f900b7eca38ffc7d1f13150e1f600d853ced3830`；规范报告 `e701eed971e196ceff44ae0123b304bc7966cddc9af8a5790334998aae45f14c`，规格报告 `ac059cbb0df22db1e84ea2cc5f0407d80dbc5a318ead0a8afa5c45cbb0544dd7`。准确集成提交的完整 CI 尚待核验，不据本记录合并或关闭 Issue。


## 总集成 CI 终态

最终源e6b9dd8精确整合为c36e7f3后，`df109015b3a78521c26c3b0f6129b276567d3ea5` 的[PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38975957143)和[push CI](https://github.com/sawaki-zexi/meido/actions/runs/38975952829)全部job成功。原完整日志各只下载一次，各后端2610 passed/39 skipped/2 warnings（515.00秒、614.71秒），前端各252 passed/44文件、TypeScript/Vite构建通过。准确df源码包含本修复；这些结果不追认旧7b已取消push为成功，也不证明所有旧挂起都只有这一根因。

原日志 `D:/meido-artifacts/shiori-ci-df10901-38975957143-full-v1.log` SHA256 `d8b30b71aa86597d4d235d7ec5f1cb1d37da1a23190f799e4e0b47f45c768250`；push原日志 `shiori-ci-df10901-38975952829-full-v1.log` SHA256 `03a491628d77c54beddd2f9c7c8f2b76367a0fafa28d5672c425e798f533b462`。各原watch已自然终止，无重复运行。
