# 四个 Skill 的真实 CLI 验收

状态：当前实验记录
最后核验：2026-10-16（Windows）
事实来源：公开 ShellTool、真实 HTTP/SkillRegistry、固定 npm 包及独立运行证据。它不表示后续四个 Skill、MCP、主动行为和 Drift 已完成。

## 固定依赖

- Shiori：`4f266aeaf42fcd611362e2456417b91f659341cf`，四份 SKILL.md 按 Meido frontmatter、workspace 和权限改写，MIT 来源保留。
- Node `24.15.0`；curl `8.21.0`（Schannel）；`@steipete/summarize@0.25.2`。
- npm 包声明 dist integrity：`sha512-RUeaVcPJ23UrOB9qdfVGEmaRq2Iq2iJBufwgZz8nz2YO8GLhr/STYb+78k5nzOIVRRQ32YtQGGQK/36UhX6RQg==`。隔离前缀安装，不执行 npm lifecycle scripts，不修改应用依赖或全局命令。
- 依 [CLI 官方安装说明](https://github.com/steipete/summarize/blob/main/docs/install.md) 核验 Node 要求，再以实际 `--version` / `--help` 为版本行为依据。

## 观察结果

| 公开入口 | 实际结果 |
| --- | --- |
| ShellTool → curl wttr.in compact | `Shanghai: ☁️ +21°C`，exit 0，未截断；仅为当次观测 |
| ShellTool → Open-Meteo | 真实 JSON，包括 Shanghai 时区、current 时间和 20.8°C，exit 0 |
| ShellTool → summarize example.com | `--extract --json --timeout 20s --firecrawl off`，exit 0，正文与 Example Domain 标题；所有 API key flags 为 false |
| ShellTool → summarize notes.txt | 常规 `--json`，exit 0，真实文件 marker 出现在结果；`llm:null`，无模型凭据 |
| HTTP 激活 summarize → 真实 CLI → 本地兼容服务 → 下一模型请求 | 快照确为 builtin summarize；CLI 读取源 marker，本地服务收到一条真实请求，下一模型请求含实际摘要，最终答复完成；2 次 Meido 模型请求 |

HTTP 摘要模型使用隔离 localhost fixture 与无效的测试 token；没有付费模型调用。HOME/USERPROFILE/TMP、角色数据、CLI 缓存和配置均隔离，普通宿主密钥未进入子进程。

## 红绿与版本差异

实际首次运行暴露 Windows 环境变量问题：Python `os.environ.items()` 给出大写 `SYSTEMROOT`，旧 Shell 的 `SystemRoot` 精确比较遗漏它。相同 argv 的控制组缺根变量时 Node 加密模块 exit 134、curl DNS exit 6，仅保留 SYSTEMROOT 后均 exit 0。修复只让原允许系统变量按大小写兼容匹配；未增加任意环境或凭据。公开 Shell 子进程回归先红（missing）后绿（真实根路径），密钥仍 absent。

0.25.2 的 txt `--extract` 实际 exit 1，不能按旧 Skill 宣称支持；正文和手册改为常规本地文件模式。CLI 短内容直接返回不等于模型推理，因此另用 `--force-summary` 和本地服务验证完整摘要请求。wttr.in 完整 j1 曾超过展示预算，记录为截断；单独 compact 查询成功且完整。

其他公开红绿验证：文件工具创建新技能后下一回合显式激活；正文中的 `$skill` 能在正常聊天触发；表情 manifest/真实图片进入下一模型请求；缺依赖具体诊断；角色版本覆盖内置；scripts 支持资源变化后拒绝加载。

独立审查后补公开 Registry 回归：真实 Windows role/workspace 祖先 junction，在发现前建立或发现后替换四种情况均先红后绿；未将该证据扩大为 HTTP 逃逸，RoleStore 另有路径检查。公开描述符拒绝路径/带值依赖，原共享列表与导出诊断不再反向修改冻结快照。

## 可复核证据

本次本地证据目录：`C:/Users/Elysia/.codex/visualizations/2026/10/15/01a13e82-f9da-7eb1-8b64-ae6ab21c48d0/skills-152-qa/`。

| 文件 | SHA256 |
| --- | --- |
| http-native-evidence.json | `e98a41a72085f88a4b67184fe038ea7600d33775a6bea17361a594b2be3fc4ff` |
| weather-compact-evidence.json | `bbcd2bb3922a4d2322101cc622b0f3e346bea66036ecc502688bf0d515430802` |
| env-diagnosis.json | `e66ffd975010905daa926d470f3fde88507c9b3569203d0a8c35d68d096e058b` |

manifest.json 另记录完整 argv、版本、其他原始证据与 runner hash。原始输出和资源保留在独立目录，未提交 npm 依赖、用户配置或大体积运行产物。可维护复验命令见[手册](../handbook/shiori-builtin-skills.md)。
