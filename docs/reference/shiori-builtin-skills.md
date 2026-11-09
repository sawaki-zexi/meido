# Shiori 内置 Skill 契约

状态：当前
最后核验：2026-11-09（普通技能发现、管理与模型按需选择）
事实来源：[SkillRegistry](../../backend/app/agent_runtime/skills.py)、[技能库](../../backend/app/skill_library.py)、[受控读取](../../backend/app/agent_runtime/skill_discovery.py)、[公开行为测试](../../tests/python/test_skill_discovery.py)、[生命周期测试](../../tests/python/test_skill_lifecycle.py)。

当前内置 `weather`、`summarize`、`feed-manage`、`meme-manage`、`skill-creater`、`create-drift-skill`、`codex-delegate`、`create-proactive-source`。未迁移角色保留旧显式规则；逐角色启用后，这些已审阅的方法进入自动候选。普通聊天输入 `$weather` 等 ID 可直接指定；HTTP 的 `skillIds` 保持兼容。Skill 是指导与资源，不授予执行工具或权限。

另提供 `conversation-recall` 与 `conversation-continuity` 两份聊天技能，参考维护者提供的角色和技能材料改写。
未迁移角色使用原有 `keyword` 激活；迁移后按当前角色模型的任务判断选择，不再同时强制关键词加载。仍支持 `$conversation-recall` / `$conversation-continuity` 显式选择。
两份技能的 `tools` 均为空，不加载或授予额外能力，详见[聊天行为契约](chat-behavior.md)。

本页列普通聊天的内置 Skill。Drift 表情维护已有独立适配与许可，见[维护契约](drift-meme-maintenance.md)；不能从普通目录或 QQ 收录入口推导其自主权限。

## 发现与覆盖

| 来源 | 位置 | 同名优先级 |
| --- | --- | --- |
| 当前角色 | `<role>/workspace/skills/<id>/SKILL.md` | 最高；文件工具可以创建 |
| 当前角色旧目录 | `<role>/skills/<id>/SKILL.md` | 同来源中次于 workspace |
| 主人公共库 | `<data>/shared-skills/<id>/SKILL.md` | 次于角色，高于项目；主人明确发布后对所有角色可见 |
| 项目 | `.agents/skills/<id>/SKILL.md` | 次于角色 |
| 内置 | `backend/app/builtin_skills/<id>/SKILL.md` | 兜底 |

`discover_many` 对 installed、external 来源保留显式激活的信任限制，优先级位于 project 与 builtin 之间。被覆盖的描述符记录 `shadowed` 和实际选择来源；同一个根内重复 ID 仍是配置错误。覆盖不会把角色/外部内容提升为 builtin 信任级别。

发现保留描述符和 hash；激活才读取正文并验证。当前运行的快照不随文件写入更新，新建技能在下一次对话发现。正文不存入运行快照，描述符、hash、资源清单和诊断保留。

## frontmatter

既有 `id/version/description/tools/activation` 保持兼容；也接受最小 `name/description`，默认版本 `1.0.0`，名称作为稳定 ID。有独立 `id` 时 `name` 可为展示名称。新格式默认 `activation: model`；旧格式未写 activation 时仍默认 explicit。支持布尔 `allow_implicit_invocation` 或 `policy.allow_implicit_invocation`，false 由宿主强制仅手动；与显式 activation 矛盾时拒绝。外部宿主专用字段通过 `ignoredMetadata` 具体列出且不生效。描述最多 2,000 字符，详细步骤留在正文。可选依赖列表：

| 字段 | 含义 |
| --- | --- |
| `requires.bins` | 当前宿主 PATH 可找到的普通命令名；不得使用路径或命令片段 |
| `requires.env` | 当前宿主显式向执行上下文暴露的变量名；只接受名称，不接受值 |

两者最多各 32 项，每项最多 128 字符，不得重复。支持 YAML 行列表、内联列表及 `requires: {bins: [], env: []}`；安全解析拒绝重复键、别名与可执行标签。仅解释支持的字段和类型，metadata、homepage 等不授予能力。

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
| meme-manage | 当前角色 workspace 文件工具维护 manifest；QQ 主人显式回合另有当前附件收录与原群复用的窄适配，复用文件/发送权限，见[操作手册](../handbook/shiori-builtin-skills.md)；其他二进制复制沿用获准命令 |
| skill-creater | 当前角色文件工具；创建合法 SKILL.md、参考/资源/脚本并读回验证，下一次对话按角色策略复用 |
| create-drift-skill | 当前角色文件工具与明确Drift作者范围；产物经信任及独立执行许可后，由后续合格Drift同hash实际消费，见[操作手册](../handbook/drift-skills.md) |
| codex-delegate | 独立 Codex 配置及当前仓库、CLI 与 spawn coding 许可；受管后台任务、精确 session 继续和原来源结果；不新增普通工具，见[操作手册](../handbook/codex-delegate-acceptance.md) |
| create-proactive-source | 当前角色文件工具；创建真实stdio来源、配置和验证草稿，经owner预览/trust/具体grant/受管验证后由真实tick消费并持久ACK；见[操作手册](../handbook/proactive-source-authoring.md) |

summarize 的 Windows `.cmd` 仅作为真实 npm 安装的 PATH 发现入口；执行使用 Node 和实际 `dist/cli.js`，不经 cmd.exe。CLI 的 `--extract` 支持 URL；本地纯文本使用常规文件模式加 `--json`。`llm:null` 表示可能直接返回短文本，不表示已完成模型调用。完整配置与验收见[操作手册](../handbook/shiori-builtin-skills.md)。

Feed 的服务依赖通过真实 MCP 目录/连接诊断，不要求服务 Python 位于 Meido PATH，也没有新增 `requires.mcp`。七动作、名称批量删除风险和旧缓存的新鲜度限制见 [Feed 契约](feed-skill.md)。

## 目录、读取与管理接口

启用自主选择的主人来源提供常驻 `skill_list(offset?, limit?, query?)` 与 `skill_read(ref, resource?)`。初始目录只含自动候选的名称、描述、来源引用、策略与具体缺项，受容量 2% 或未知容量 8,000 字符上限约束；`incomplete/nextOffset` 明示分页。手动项不能自行读出；明确指定的可用项在首次请求前完整加载，未知/停用/缺依赖分别说明。

引用固定角色、来源和文件身份；读取和恢复同时检查初始策略与当前授权。正文与资源完整返回或明确拒绝超预算，资源须来自正文读取后返回的索引，不开放宿主路径。真实读取记录在工具结果 `details.skillRead` 和运行 `capabilities.skillReads` 中；已存在有效窗口的同 hash 正文不重复插入，压缩后只恢复本轮实际加载且仍合法的指导。工具授权与业务动作不恢复或重放。

管理接口为 `/api/roles/{roleId}/skills`：GET 列来源、覆盖、状态与迁移预览；POST 保存并读回核验；`GET /document?ref=...` 读取正文；`PUT /settings` 保存角色开关和单项策略（必须携带 `expectedRevision`）；`POST /remove` 与 `/publish` 必须携带 `ref/expectedHash`。编辑正文同样需要当前 hash，冲突 409 保留草稿。公共发布只写数据目录，资源保留，内置与项目文件只读。无共享发布模型工具。操作见[手册](../handbook/ordinary-skills.md)。

聊天 `read_file` 对普通技能正文/资源返回实际读取字节的 `contentHash`，并由宿主按当前 role/session/run 保存版本；`write_file`/`edit_file` 更新已有普通技能文件必须匹配该本轮版本，冲突为失败回执 `error_code: conflict`。最终比较和提交与管理保存共用事务锁；新建文件及非技能文件兼容原行为。图片资源的回执只持久保存类型和 hash，只有当前窗口存在匹配的真实图片块时才能复用，历史文字标记不表示图片仍可见。
