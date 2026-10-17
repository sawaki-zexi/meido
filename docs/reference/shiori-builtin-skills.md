# Shiori 内置 Skill 契约

状态：当前
最后核验：2026-10-17
事实来源：[SkillRegistry](../../backend/app/agent_runtime/skills.py)、[宿主装配](../../backend/app/main.py)、[公开行为测试](../../tests/python/test_builtin_skills.py)、[HTTP 测试](../../tests/python/test_builtin_skills_api.py)。

当前内置 `weather`、`summarize`、`feed-manage`、`meme-manage`、`skill-creater`、`create-drift-skill`、`codex-delegate`、`create-proactive-source`，均显式激活。普通聊天输入 `$weather` 等名称即可选择；HTTP 的 `skillIds` 保持兼容。Skill 是指导与资源，不是执行工具或权限授予。

本页列普通聊天的八份内置Skill。Drift有独立适配与许可，不能从普通目录推导其全部可执行；当前内置meme-manage的永久依赖缺口由[#191具体规格](../specs/drift-meme-maintenance-spec.md)追踪，方案尚未获批或实施。

## 发现与覆盖

| 来源 | 位置 | 同名优先级 |
| --- | --- | --- |
| 当前角色 | `<role>/workspace/skills/<id>/SKILL.md` | 最高；文件工具可以创建 |
| 当前角色旧目录 | `<role>/skills/<id>/SKILL.md` | 同来源中次于 workspace |
| 项目 | `.agents/skills/<id>/SKILL.md` | 次于角色 |
| 内置 | `backend/app/builtin_skills/<id>/SKILL.md` | 兜底 |

`discover_many` 对 installed、external 来源保留显式激活的信任限制，优先级位于 project 与 builtin 之间。被覆盖的描述符记录 `shadowed` 和实际选择来源；同一个根内重复 ID 仍是配置错误。覆盖不会把角色/外部内容提升为 builtin 信任级别。

发现保留描述符和 hash；激活才读取正文并验证。当前运行的快照不随文件写入更新，新建技能在下一次对话发现。正文不存入运行快照，描述符、hash、资源清单和诊断保留。

## frontmatter

既有 `id/version/description/tools/activation` 不变，增加以下可选列表字段：

| 字段 | 含义 |
| --- | --- |
| `requires.bins` | 当前宿主 PATH 可找到的普通命令名；不得使用路径或命令片段 |
| `requires.env` | 当前宿主显式向执行上下文暴露的变量名；只接受名称，不接受值 |

两者最多各 32 项，每项最多 128 字符，不得重复。支持 YAML 风格行列表或内联列表，不接受任意 YAML 对象。Shiori 的 `name/metadata/homepage` 不属于 Meido 格式。

文件解析和公开 SkillDescriptor/Plugin 构造均校验依赖并复制为不可变 tuple。诊断嵌套数组同样冻结；导出快照返回新数组，不能反向改变已提交版本。

`requires.env` 不读取或继承任意后端环境。`SkillRegistry.resolve(available_env=...)` 只接受宿主已许可的名称；当前聊天装配不暴露额外凭据变量，CLI 自身配置是独立路径。声明 `OPENAI_API_KEY` 等名称不会获得 Meido 模型密钥。

选中的技能缺依赖时不注入正文，返回 `status:unavailable`、`missingBins`、`missingEnv`，并把具体缺项加入模型上下文。变量值不进入诊断。工具缺失或关闭仍按现有能力策略过滤，Skill 不能重新启用 Shell、文件、渠道或其他工具。

`references/`、`assets/`、`scripts/` 下支持文件均验证范围、大小和 hash；每个文件最多 256 KiB，符号链接、Windows junction 与越界资源拒绝。发现保留原始绝对路径，在发现和激活时复查完整祖先链，角色目录或 workspace 的链接也不能绕过。脚本只是支持资源，不自动执行；普通对话使用默认平台 Shell，仍受预算和真实来源限制；受限任务另须独立白名单。

## 内置流程

| Skill | 依赖与实际结果 |
| --- | --- |
| weather | curl + 角色 Shell；wttr.in 优先，Open-Meteo 备用，无 API key |
| summarize | npm 安装的 summarize + Node 24+ + 角色 Shell；本批核验 CLI 0.25.2，服务凭据独立配置 |
| feed-manage | 主人已配置并授权的 MCP；按实际 serverId、工具名和 schema 管理订阅与查询文章；不自动推送 |
| meme-manage | 当前角色 workspace 的文件工具；维护 manifest 与真实图片，复制二进制另需获准命令 |
| skill-creater | 当前角色文件工具；创建合法 SKILL.md、参考/资源/脚本，下一次对话显式激活 |
| create-drift-skill | 当前角色文件工具与明确Drift作者范围；产物经信任及独立执行许可后，由后续合格Drift同hash实际消费，见[操作手册](../handbook/drift-skills.md) |
| codex-delegate | 独立 Codex 配置及当前仓库、CLI 与 spawn coding 许可；受管后台任务、精确 session 继续和原来源结果；不新增普通工具，见[操作手册](../handbook/codex-delegate-acceptance.md) |
| create-proactive-source | 当前角色文件工具；创建真实stdio来源、配置和验证草稿，经owner预览/trust/具体grant/受管验证后由真实tick消费并持久ACK；见[操作手册](../handbook/proactive-source-authoring.md) |

summarize 的 Windows `.cmd` 仅作为真实 npm 安装的 PATH 发现入口；执行使用 Node 和实际 `dist/cli.js`，不经 cmd.exe。CLI 的 `--extract` 支持 URL；本地纯文本使用常规文件模式加 `--json`。`llm:null` 表示可能直接返回短文本，不表示已完成模型调用。完整配置与验收见[操作手册](../handbook/shiori-builtin-skills.md)。

Feed 的服务依赖通过真实 MCP 目录/连接诊断，不要求服务 Python 位于 Meido PATH，也没有新增 `requires.mcp`。七动作、名称批量删除风险和旧缓存的新鲜度限制见 [Feed 契约](feed-skill.md)。
