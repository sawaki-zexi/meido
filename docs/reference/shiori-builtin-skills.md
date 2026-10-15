# Shiori 内置 Skill 契约

状态：当前
最后核验：2026-10-16
事实来源：[SkillRegistry](../../backend/app/agent_runtime/skills.py)、[宿主装配](../../backend/app/main.py)、[公开行为测试](../../tests/python/test_builtin_skills.py)、[HTTP 测试](../../tests/python/test_builtin_skills_api.py)。

本批内置 `weather`、`summarize`、`meme-manage`、`skill-creater`，均显式激活。普通聊天输入 `$weather` 等名称即可选择；HTTP 的 `skillIds` 保持兼容。Skill 是指导与资源，不是执行工具或权限授予。

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

`requires.env` 不读取或继承任意后端环境。`SkillRegistry.resolve(available_env=...)` 只接受宿主已许可的名称；当前聊天装配不暴露额外凭据变量，CLI 自身配置是独立路径。声明 `OPENAI_API_KEY` 等名称不会获得 Meido 模型密钥。

选中的技能缺依赖时不注入正文，返回 `status:unavailable`、`missingBins`、`missingEnv`，并把具体缺项加入模型上下文。变量值不进入诊断。工具缺失或关闭仍按现有能力策略过滤，Skill 不能重新启用 Shell、文件、渠道或其他工具。

`references/`、`assets/`、`scripts/` 下支持文件均验证范围、大小和 hash；每个文件最多 256 KiB，符号链接与越界资源拒绝。脚本只是支持资源，不自动执行；执行仍须现有 Shell 白名单和预算。

## 四份流程

| Skill | 依赖与实际结果 |
| --- | --- |
| weather | curl + 角色 Shell；wttr.in 优先，Open-Meteo 备用，无 API key |
| summarize | npm 安装的 summarize + Node 24+ + 角色 Shell；本批核验 CLI 0.25.2，服务凭据独立配置 |
| meme-manage | 当前角色 workspace 的文件工具；维护 manifest 与真实图片，复制二进制另需获准命令 |
| skill-creater | 当前角色文件工具；创建合法 SKILL.md、参考/资源/脚本，下一次对话显式激活 |

summarize 的 Windows `.cmd` 仅作为真实 npm 安装的 PATH 发现入口；执行使用 Node 和实际 `dist/cli.js`，不经 cmd.exe。CLI 的 `--extract` 支持 URL；本地纯文本使用常规文件模式加 `--json`。`llm:null` 表示可能直接返回短文本，不表示已完成模型调用。完整配置与验收见[操作手册](../handbook/shiori-builtin-skills.md)。
