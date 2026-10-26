# 工具加载瘦记录离线测量

状态：已测量的合成夹具，不代表真实模型/MCP 事务或性能 SLA。
最后核验：2026-10-26；实现基线 `fcb92bd` 加已合入的检索/完成性依赖。
事实来源：[可复跑脚本](../../scripts/benchmark_tool_loading.py)、[全部 51 行原始结果](tool-loading-benchmark-2026-10-26.json)、[安全/兼容回归](../../tests/python/test_lightweight_tool_loading.py)。

## 范围与方法

```sh
python scripts/benchmark_tool_loading.py --output /tmp/tool-loading.json
```

Python 3.12.14 / SQLite 3.53.1 / Linux x86_64；固定种子标识 234，工具名、schema 与调用顺序完全确定。新路径实测 `50/500/2000` 工具 × 每 schema 描述 `1/10 KiB` × `0/1/10/30` 次加载请求 × `20/100` 个 audit，共 48 场景。工具 execute 一旦被调用就抛错；只调用本地搜索、普通结果持久化、内存发布、call audit 和完成缓存。每场景独立临时数据库，场景结束只清理其夹具。

额外 3 场景从固定 `8a2d27d` 提取原 SessionStore 源码，实际执行旧完整目录 revision/call SQL：50 工具、1 KiB、0/1/10 次加载、20 calls。选择/预算与其他代码仍使用当前夹具，**不是完整旧 Runtime 的端到端时间比较**。未执行旧大矩阵，避免为测量制造多 GB 的重复目录写入；未用理论估算伪装测得吞吐。

P50/P95 是少量样本的经验统计：请求准备 5 次；resolve/restore 3 次（其 P95 取最大值）；audit 按 20/100 次。共享开发容器存在其他工作，延迟只描述这次实验，不作准确提速百分比。restore 计时指同进程 `registry.resolve(restored_activation=cache)` 的当前定义/许可重新验证，不是冷进程启动或数据库恢复耗时；后者此次未测。完整脚本峰值 RSS 为 186620 KiB，不是每场景净增量。

## 可重复的存储性质

固定 10 次加载、20 calls：

| 路径 | 候选工具 | 每 schema | run metadata bytes | audit payload bytes | 单次 binding bytes | WAL 文件 bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 新 | 50 | 1 KiB | 1812 | 32680 | 1632 | 457352 |
| 新 | 500 | 1 KiB | 1812 | 32680 | 1632 | 457352 |
| 新 | 2000 | 1 KiB | 1812 | 32680 | 1632 | 457352 |
| 新 | 50 | 10 KiB | 1812 | 217000 | 10848 | 1079472 |
| 新 | 500 | 10 KiB | 1812 | 217000 | 10848 | 1079472 |
| 新 | 2000 | 10 KiB | 1812 | 217000 | 10848 | 1079472 |
| 旧 SessionStore | 50 | 1 KiB | 926805 | 40 | 全目录存于 run | 4799832 |

这个夹具直接证实：增加**未使用候选**不再扩大每个调用的持久 payload。使用的 schema 本身变大仍扩大 call binding，这是保留可解析所用定义的必要成本。没有为了减少数字删掉来源、作者摘要或使用定义，也没有保存参数/结果的第二份副本。

WAL 数字是文件已分配长度，不等于累计 SQLite 逻辑写入。`/proc/self/io` 的 write_bytes 增量在此工作区全部为 0；不能据此说“没有磁盘写入”，该底层计数在当前存储环境不提供有效物理 I/O 证据。报告保留原值与局限。

## 时间与界限

同一 10 loads / 20 calls 的 1 KiB 场景，新请求 schema 准备 P50 约 0.029–0.030 ms；audit 写入 P50 0.246–0.370 ms。50 工具旧 SQL 场景 audit P50 10.978 ms / P95 15.041 ms；仅说明重复 JSON 更新在此小基线的代价，不能泛化为模型响应速度。

冻结准入/恢复仍扫描当前候选，不是常数：2000 × 10 KiB、10 loads 场景 resolve P50 282.914 ms、restore P50 287.683 ms。优化的是持久重复写入，不声称去掉了 registry 检查成本。累计预算会拒绝过大的加载，原始数据同时列 requested_loads 与 selected，不能把 30 个加载请求误称为都已加载。

安全门槛由实际发现、来源 ABA、作者、委托与不确定回执测试证明，benchmark 不替代这些测试。没有真实账号、MCP、模型费用、自动重放或历史数据删除。
