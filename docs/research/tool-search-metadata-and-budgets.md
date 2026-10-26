# 工具检索元数据与声明预算：离线证据

状态：当前实验记录（不代表线上速度或召回保证）

最后核验：2026-10-26。Meido 基线 `8a2d27d4e0bcab11bb1d7dfd6c788997d90ca750`；方案代码/fixture `51dd2a4e2ec7cbedc6e8161dbd9585799982d7af`（其父 `0dd84f6` 为搜索与预算实现）。

事实来源：[纯元数据提取](../../backend/app/agent_runtime/search_metadata.py)、[声明计量](../../backend/app/agent_runtime/search_budget.py)、[评测程序](../../scripts/evaluate_tool_search.py)、[固定语料](../../tests/fixtures/tool_search/quality.json)、[完整实测 JSON](fixtures/tool-search-235-evaluation.json)、[公开边界测试](../../tests/python/test_tool_search_budgets.py)、[质量门槛测试](../../tests/python/test_tool_search_quality.py)。

## Pi 取舍：学字段，不照搬 tokenizer 或新状态系统

本次开发继续检查 Pi 固定 `d5b3de22d0e24a3e9c38a9410d3329b099717283`：

- `packages/coding-agent/src/extensions/tool-search/tool.ts` 的 `schemaText` 提取 description/property names，递归 properties/items/anyOf/oneOf/allOf；没有遍历深度/节点/字节限额。namespace instructions 被当检索文本。
- 同文件 tokenizer 用非 ASCII 字母数字分隔，纯中文没有 term；BM25 并不能弥补输入丢失。tool_search 只选尚未声明候选，没有累计 schema 预算。
- `packages/coding-agent/src/extensions/mcp/index.ts` 独立呈现 namespace 小地图，摘要/完整 section 按字符截断；Meido 改为 UTF-8 bytes，并只使用宿主核验的来源和短元数据，不提升远端 instructions 的权威。

因此保留原 Meido 名称/提示/说明权重与 CJK terms，新增参数权重 3 和**每候选合计最多 2** 的 namespace 证据。未引入 BM25、embedding、外部服务、codemode、永久索引或第二授权目录。每次从当前已授权定义生成小文档，不需要新增缓存失效链。以下结果支持元数据改动，不是 BM25 与其他算法孰优的实验。

## 语料和预先固定的门槛

60 条人工整理的工具任务意图：20 中文、15 中英混合、15 英文、10 空/无关。不是生产用户日志，不包含真实账号/凭据。46 个固定定义中，41 个直接来自上述 Meido 基线的群资料、消息、附件、记忆、调度、文件及浏览器工具；5 个是明确标注的去敏 MCP 协议/安全干扰 fixture，不冒充实际部署服务。

每条有 relevant/forbidden 标签，含 schema-only、namespace 共用词、近名、风险收紧和没有词面对应的同义意图。拆分先于比较：40 dev，20 分层 holdout（中文7、混合5、英文5、空/无关3），seed 235。参数/namespace 权重在首次 holdout 前已经固定；看完 holdout 后没有调权重、换 query 或修改相关标签。

硬门槛：forbidden/无权 namespace/风险曝光为零，holdout Recall@5、MRR 不低于原 scorer，无关查询误加载率不高于基线。MRR 用完整固定候选排名；JSON 同时报告 MRR@5。空/无关只进入误加载率，不能冲淡正例召回率。每次候选耗尽单列为增长实验，不混入固定语料质量分数。

| split/方案 | Recall@3 | Recall@5 | MRR | 无关误加载 |
| --- | ---: | ---: | ---: | ---: |
| dev 原 scorer | 0.7727 | 0.7727 | 0.7922 | 0/7 |
| dev +元数据 | 0.9545 | 1.0000 | 0.9758 | 0/7 |
| holdout 原 scorer | 0.6471 | 0.6471 | 0.6471 | 1/3 |
| holdout +元数据 | 0.8824 | 0.9412 | 0.8676 | 1/3 |
| holdout 去参数 | 0.7647 | 0.7647 | 0.7451 | 1/3 |
| holdout 去 namespace | 0.8529 | 0.9118 | 0.8647 | 1/3 |
| holdout 去 search_hint | 0.8824 | 0.9412 | 0.8088 | 1/3 |
| holdout 去描述 | 0.9412 | 0.9412 | 0.8824 | 0/3 |

所有方案 forbidden/无权 namespace/风险命中均为 0；当前方案通过预定门槛。全60条无关误加载为1/10，并非零：原有 CJK 单字匹配仍会命中“zzzz不存在”中的常用字。纯同义英文 `remind` 对 `schedule` 仍缺词面证据、未命中。小语料中的描述消融更优不等于可安全删除生产描述；没有据此调 holdout。不得把这里的改善推广成中文真实流量的效果保证。

真实参数新增示例：`agent_browser_type` 的 description/search_hint 没有 `keystrokes`，但 schema `delayMs.description` 有；原 scorer 返回空，新方案第一名为该工具。namespace-only “财务核算”可找到两个同服务 fixture；真正的参数证据可区分它们，名称精确选择仍优先于共享摘要。

## 体积、重建与增长

44 个获授权 fixture 的完整 neutral declaration：最小138 B，中位407 B，P95 1055 B，最大1115 B，总20604 B，超过32 KiB者为0。这个样本没有显示默认限额会拒绝普遍工具；并未声称覆盖所有生产 MCP schema。

固定 seed，从上述定义采样并重命名为50/500/2000条。重建7次；5条中英/空匹配 query 各重复4次。以下是本地单次实验，受机器负载影响；不是 SLA，也没有同环境旧版端到端计时，所以**不宣称提速百分比**。

| 定义数 | 派生文本 bytes | 重建 P50/P95 ms | 已建文档排名 P50/P95 ms | 重建+排名 P50/P95 ms |
| ---: | ---: | ---: | ---: | ---: |
| 50 | 15730 | 1.408 / 1.539 | 0.198 / 0.360 | 1.545 / 2.045 |
| 500 | 150988 | 14.773 / 16.906 | 1.499 / 3.430 | 17.398 / 28.131 |
| 2000 | 591091 | 57.265 / 80.872 | 5.523 / 10.600 | 67.228 / 78.974 |

生产实现每次重建；“已建文档排名”仅用于分离成本，不代表已经实现缓存。1,000,070 B 的巨大完整声明仍整项计量并拒绝，派生文档只保留8 KiB以内，提取0.104 ms、返回 `search_text_truncated`。深/宽/循环图、UTF-8边界、$ref/default/examples不索引有独立测试，不联网解析。

增长实验用50个约10KiB的完整 schema，连续20次宽泛查询：前四次加载3、6、9、12个，总字节30411、60822、91233、121644；第5–20次保持12个/121644 B，明确拒绝下一批，旧工具不卸载。重复 exact 已加载定义新增0 B；所有候选已加载时关键词结果为空。完整报告保存每一步激活名称、跳过名称和字节值。

同请求预留/释放、输出裁剪与实际暂存一致、必需 baseline 例外、当前风险/权限先过滤由公开单元测试覆盖；持久化失败、same-reply、恢复/重启及 MCP 生命周期由工具发现/加载集成测试覆盖，不把纯排名基准冒充这些宿主测试。

## 复现

```sh
python -m pytest tests/python/test_tool_search_budgets.py tests/python/test_tool_search_quality.py tests/python/test_tool_search_relevance.py tests/python/test_tool_discovery.py -q
python scripts/evaluate_tool_search.py --split dev
python scripts/evaluate_tool_search.py --split all --benchmark --output /tmp/tool-search-evaluation.json
```

开发过程先对缺失元数据/预算接口得到红测，再通过相关测试。没有安装新依赖、连接真实 MCP/模型、读取私人日志、消耗模型 token 或写外部事务。Provider cache、自动 unload、语义向量及更重索引仍为非目标。

## PR #244 正式评审前复核

2026-10-26 再次对照 [Pi `b84d5d1b` 的 tool-search](https://github.com/earendil-works/pi/blob/b84d5d1bc315dac7e2d384190cfad112e63deb9d/packages/coding-agent/src/extensions/tool-search/tool.ts) 和 [Shiori `d149de7f` 的 tool_search](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/tools/tool_search.py)、[search_backend](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/tools/search_backend.py)。Pi 此文件与原 `d5b3de22` 基线内容相同；Shiori 仍提供中文检索、精确加载及风险过滤。本次保留既有评分和授权边界，没有引入另一套索引或切换 BM25。

公开 `tool_search` 回归确认：原实现先截取排名前 `top_k` 项，再过滤声明预算；当第一名超过 32 KiB、`top_k=1` 时，即使下一名足够小也会返回空。现改为沿原排名扫描完整候选，超大项计入预算跳过列表，直到选满可容纳的 `top_k` 项或候选耗尽。精确名称仍只选择该名称，`select:` 保留既有顺序；当前许可、pending 去重、完整 schema 计量和输出裁剪合同继续生效。

同轮完成性审查补齐 Anthropic 未关闭参数块、Responses 混合完成/未完成项及原生截断原因；以真实适配器和本地假传输验证，详见[完成证据手册](../handbook/tool-response-completion.md)。以上修复不改变本节历史离线基准的评分器、语料或既有实测数字，也不把协议夹具视为真实账号验收。
