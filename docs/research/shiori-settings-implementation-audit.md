# Shiori 设置实现审计与 Meido 复刻边界

状态：当前调研；本文不是已实现功能清单，也不授予架构或外部事务许可。
最后核验：2026-10-19。
事实来源：Shiori 固定提交 [076a87b（2026-10-17）](https://github.com/YinFengWindy/Shiori-Agent/commit/076a87b3b8df20140b9c4b683eeed2ff2a9db84a)；历史参照 4f266aeaf42fcd611362e2456417b91f659341cf；Meido 对照提交 [6035dfd](https://github.com/sawaki-zexi/meido/commit/6035dfddeb7a9803d62f5a074d47569c59955723)，分支 feat/137-default-builtin-tools、Draft PR #196、总目标 #137。工作树后续实现应另在规格或交付记录更新，本文对照不随实现自动成立。

## 结论

**已提取**：Shiori 的设置由七个内置领域组成：模型、记忆、外观、高级、我的身份、插件、关于；插件也能注册设置领域或插件内子页。高级真实有十三项，而 Meido 6035dfd 的高级仅有折叠的角色执行预算与工具诊断。因此此前“七类侧栏、配色、开关已复刻”只覆盖展示框架，没有完成设置的运行语义复刻。[注册入口](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/registerBuiltinSettingsSections.tsx#L29)、[高级字段](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/advancedSettingsFields.ts#L20)、[Meido 高级](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/AdvancedSettings.tsx#L5)。

**推断**：完整复刻必须逐项证明“修改 → 校验 → 持久化 → 运行消费者 → 重开或重启仍一致 → 失败可恢复”，而不是添加同名输入框。模型容量、上下文压缩、可替换记忆引擎、动态插件包、语音服务和桌面更新都需要对应运行能力。现有多协议模型适配、角色数据归属和已批准的费用边界应保留；不能为外观一致删掉 Meido 已有能力。

## 一、页面结构与保存协议

| 层级 | Shiori 行为与源码 |
| --- | --- |
| 一级导航 | 七栏顺序固定，注册式扩展，不是每个插件挤进主侧栏。[内置注册](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/registerBuiltinSettingsSections.tsx#L29) |
| 二级导航 | 模型“模型注册”；记忆“基础 / 向量模型”；外观“显示”；高级“基础”；身份“已绑定”；插件“已安装”；关于“应用更新”。单个子页不用伪造多余页签。[注册](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/registerBuiltinSettingsSections.tsx#L29) |
| 插件详情 | 从插件“设置”进入带返回按钮的子页；不会把每个插件配置变成插件列表旁边的页签。[SettingsPage](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/SettingsPage.tsx#L102) |
| 子页记忆 | 应用外壳记录每栏最后子页，切入独立页再返回仍保留；插件关闭时立刻退回可见子页并写回该选择，重新启用不会突然跳回失效子页。[子页状态](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/useSettingsSubsectionMemory.ts#L20) |
| 普通设置 | 模型 / 记忆 / 外观中的流式开关 / 高级共用设置草稿；读取 snapshot + generation，修改草稿后交串行保存队列。[控制器](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/useSettingsPageController.ts#L14) |
| 独立数据域 | 身份、插件和关于不依赖普通设置读取成功；外观的看板娘与背景动效直接使用 renderer 本地偏好。[分支](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/SettingsPage.tsx#L72)、[外观](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/AppearanceSettingsSection.tsx#L15) |

保存不是“每个 onChange 并发 PUT”：队列串行化、合并被新编辑覆盖的未发草稿、保存中保留更新后的编辑，不让旧响应覆盖新草稿。普通 SettingsSaveQueue 没有传 debounceMs，实际使用队列默认的即时调度；SDK 的 400ms 常量用于插件私人自动保存等其他调用，不能一概宣称所有设置都 400ms 防抖。[普通队列](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/settingsSaveQueue.ts#L30)、[SDK 队列](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/packages/sdk/src/serialDraftQueue.ts#L13)。

失败区分校验错误、运行应用失败、传输结果不明、写入成功但回读失败：失败草稿保留；重试沿用原 operationId；结果不明不先提交新草稿；已确认写成功而回读失败时只重读，不重复应用事务。generation 冲突需要重新加载或明确处理，不静默覆盖其他写入。[保存结果](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/settingsPersistence.ts#L59)、[重试分支](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/settingsSaveQueue.ts#L60)、[后端锁与幂等](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/apply.py#L183)、[代号校验](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/apply.py#L296)。

普通设置表单仅拥有 llm / agent / desktop / memory 顶层；提交 preserve_plugins=true，后端在锁内从当前配置保留其他顶层数据，避免普通设置陈旧快照覆盖插件配置。模型删除附带的角色解绑与 config.toml 写入有 prepared / committed 日志，启动恢复中断的两文件事务。[表单所有权](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/settings_form.py#L9)、[配置事务](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/config_transaction.py#L17)。

**Meido 差距**：当前模型、向量、插件公共参数仍有明确保存按钮，分别用自己的 REST 端点，并没有 Shiori 这一整套普通设置快照和自动保存协议。它们已有部分局部版本校验和错误反馈，不能据此宣称已具备统一保存事务。[模型 submit](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/model/ModelSettings.tsx#L91)、[向量 submit](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/MemorySettings.tsx#L76)、[插件参数](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/PluginParameters.tsx#L7)。

自动保存仅是配置持久化机制，不应合并真实事务按钮的意义：测试连接可能访问外部模型服务；配对、解绑、插件包信任、启动原生服务、安装更新、真实生成与发送等动作都应保留独立触发和真实状态。Meido 既有费用许可、账号权限、仓库 / CLI 许可和精确发布批准不能因“自动保存”被隐式确认。此项为目标端迁移约束，不能反推 Shiori 有相同费用授权系统。

## 二、模型栏：不只是服务地址和密钥

### 全部可见字段与动作

| 字段或动作 | Shiori 实际含义 |
| --- | --- |
| 服务商 | OpenAI、DeepSeek、阿里云百炼、Kimi、智谱、硅基流动、OpenRouter、Ollama、自定义；预设填 provider + baseUrl，保留模型和密钥。它们通过 OpenAI 兼容 Chat Completions 访问，不等于 Meido 的多协议适配器。[预设](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/modelProviderPresets.ts#L26) |
| 服务商标识 | 仅自定义时显示；显式选择自定义后不会因当前字段恰好匹配预设又自动切回。[字段](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/ModelRegistrationFields.tsx#L30) |
| Base URL、API Key、模型 | 模型连接身份的主要内容；API Key 可为空，本地服务无需密钥。密码框有显示 / 隐藏，浏览器 autocomplete 关闭。[字段](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/ModelRegistrationFields.tsx#L51)、[密钥框](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/SettingsFieldPrimitives.tsx#L12) |
| 上下文窗口（token） | 明确正整数；新模型必须填写完整容量才进入已保存目录，不按模型名猜容量；旧不完整记录保留“需补填”，运行可用性报 connection_incomplete。[容量契约](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settingsContract.ts#L15)、[完整性](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/modelRegistration.ts#L17)、[运行选择](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/roles/model_runtime.py#L147) |
| 自动压缩阈值（token） | 可空；已填必须正整数且小于窗口。空时使用全局触发比例；不能把它当输出上限。[容量契约](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settingsContract.ts#L15) |
| 思考强度 | none / low / high / max，中文为关闭 / 低 / 高 / 最高；注册是默认值，角色对话或视觉可再覆盖。[选项](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/shared/modelEffortLabels.ts#L9)、[运行覆盖](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/roles/model_runtime.py#L65) |
| 连接测试 | 使用尚未保存的草稿独立 models.test；修改 provider / URL / key / model 令旧测试结果失效；慢旧请求不能盖住新草稿结果；测试不会保存模型。[测试协议](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/modelConnectionTest.ts#L25)、[异步归属](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/useModelConnectionTest.ts#L12) |
| 添加 / 返回 / 删除 | 新记录先留局部草稿，未填完整返回就丢弃；完成后进入普通自动保存。已保存记录删除先枚举受影响角色，对话和视觉绑定一起确认、一起解绑，绝不自动给角色改选另一连接。[新记录](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/useModelRegistrationDraft.ts#L15)、[删除计划](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/modelRegistrationRemoval.ts#L20) |

Shiori 的普通模型密钥作为字符串在受控桌面 bridge 的设置 snapshot 中读出，并以 TOML api_key 写入；密码框隐藏只是显示行为，**不是 OS 加密 vault**。插件配置另有环境变量引用保留，二者不要混淆。[设置读取](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settings.ts#L72)、[写入](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settings.ts#L156)。Meido 现有公共模型 API 只回 apiKeyConfigured，按需专门 reveal-key；该差异不应为了复刻而退化成在列表接口回传密钥。[公共序列化](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/model_config.py#L181)、[专门读取](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/model/ModelSettings.tsx#L182)。

### 容量到运行的完整链

角色模型解析将 model_context_window、model_auto_compact_token_limit、default_max_tokens、BudgetPolicy 交给 provider。每次请求的硬输入预算为：

    available = 模型窗口 - 本次输出 token 预留 - safety_margin_tokens
    trigger = min(available, 显式自动压缩阈值或 floor(模型窗口 × trigger_ratio)，比例至少为 1 token)
    target = min(available, max(1, floor(模型窗口 × target_ratio)), trigger)

available ≤ 0 时拒绝，不能先发一个注定超窗的请求。trigger / target 都以**模型窗口**为比例基数，不是以扣除预留后的 available 再乘一次比例。输出预留必须使用该次请求实际上限；包括图像、系统提示、消息和当前工具 schema 的输入估算才能影响压缩。[角色 provider](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/roles/model_runtime.py#L74)、[输入预算唯一公式](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/prompting/input_budget.py#L54)、[上下文检查入口](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/core/passive_turn/context_window.py#L54)。

**Meido 对照**：6035dfd 的 ModelConfigurationInput 只有 providerId / provider / URL / model / apiKey / api / apiOptions，没有明确容量、自动压缩阈值、注册级思考强度；模型 UI 也没有这些控件。模型删除会把应用 activeId 换成剩余第一项，这是全局默认模型语义，不是 Shiori 的逐角色解绑事务。完整等价需要新增运行预算 / 压缩链，不能把上下文窗口只存进 apiOptions。[模型契约](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/models.py#L344)、[默认切换](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/model_config.py#L291)。

可测试闭环：添加完整模型并重开 → 容量仍在；填阈值等于窗口 → 保留草稿、拒绝保存；达到实际输入触发值 → 压缩再发送，工具 schema 计入预算；删除被两个角色引用的模型 → 确认显示两者，故障注入中不出现只删注册未解绑的半事务。

### 工作摘要是持久化的上下文状态，不是本次 run 临时文本

Shiori 的工作摘要为 JSON 对象：tasks / constraints / decisions / unfinished / tool_state / entities 六个字符串，加非空 source_message_ids。所有来源必须属于本次移除消息或上一份合法摘要来源；去重后才规范化计量。缺字段、伪造 ID、截断或无效 JSON 直接失败；只有合法但过长结果允许再缩短一次。辅助摘要请求的 generation_token_limit 为配置 summary_token_limit 的两倍，通过 auxiliary 调用预算预留，而最终可提交的 JSON 摘要仍受 summary_token_limit 硬限约束；不能把两个上限混为一谈。[摘要验证](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/compaction_summary_validation.py#L11)、[辅助请求](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/compaction_summary_request.py#L34)。

摘要按 session / user / external:<thread_id> 窗口归属，和裁剪位置、summary_source_ids、window_versions、compaction_counts 一起写入 SQLite sessions.maintenance_progress，后续回合以及进程重启后读取并注入模型输入；旧摘要和新移除原文批次折叠为下一份摘要，**不是下一轮从头临时重算**。主对话窗口与每个外部线程不能混用摘要。原始消息仍保留在数据库，裁剪只是模型窗口的持久游标。[窗口键](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/maintenance_progress.py#L29)、[SQLite 列与读写](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/store/sessions.py#L97)、[摘要读取并折叠](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/compaction_summary.py#L58)、[后续注入](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/core/passive_turn/reasoner.py#L658)。

提交前准备冻结 owner / generation / window version / cursor / 消息 ID 序列 / 前缀内容 stamp，允许之后追加消息但不纳入这次 cut；摘要只在待移除消息已被记忆整理游标覆盖、完整输入重新测量可接受后，与窗口切点一起提交。失败不能先删原文或推进游标；下次从已提交进度继续，避免重复处理。[准备和提交](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/manager/window.py#L71)、[提交校验](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/manager/window.py#L172)、[控制器提交](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/compaction.py#L466)。

解绑、身份可见边界变化会使受影响窗口及摘要失效并推进 generation；新增有明确 context_since 的主人聊天可保留原 user 摘要，不能让该聊天配对前的旧消息混入。撤销消息时，仅摘要来源或已有 cut 真正受影响的窗口清零，其他窗口保留并平移下标；清空会话使整个维护代次失效。窗口现已与模型无关，不必因换模型一律丢摘要，但新请求必须用新模型预算重测。[身份归属](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/maintenance_progress.py#L147)、[撤销](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/manager/undo.py#L129)、[清空](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/manager/persistence.py#L214)、[模型无关迁移](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/session/maintenance_progress.py#L185)。

**调研中纠正的方案差异**：仅保留一次运行的临时摘要不等价于 Shiori 的会话持久工作状态。[高级设置具体方案](../specs/advanced-settings-parity-spec.md)已据此采用持久化摘要、来源 ID、窗口版本和身份失效规则，2026-10-19 获主人批准，实施状态与重启 / 撤销 / 解绑验收见该规格。本文其余 Meido 差距仍以调研基点为准。


## 三、记忆栏：引擎开关与 Markdown 默认层是两件事

| 子页 | 可见字段 | 保存与实际消费者 |
| --- | --- | --- |
| 基础 | 启用记忆；记忆引擎 | enabled 决定语义引擎是否构造、是否注册对应记忆 meta 工具；engine 空值归一化 default，选择已安装的引擎插件。UI 默认只有“默认”，会保留已有自定义引擎值，不是列出一个未经后端探测的任意引擎市场。[表单](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/MemorySettingsSection.tsx#L13)、[选项](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/settingsSectionUtils.ts#L15)、[插件装载](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/memory_plugins.py#L15) |
| 向量模型 | 模型；API Key；Base URL；输出维度 | 维度可空，非空必须正整数；embedding 配置独立于聊天注册。UI placeholder text-embedding-v4 不等于后端 MemoryEmbeddingConfig 默认值 text-embedding-v3。[表单](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/MemorySettingsSection.tsx#L23)、[维度校验](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settings.ts#L258)、[后端默认](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/config_models.py#L13) |

默认 Markdown 记忆层**始终构造**，enabled=false 仅换成 DisabledMemoryEngine；不代表停掉所有 Markdown 记忆维护。memory_window 还会影响 Markdown keep_count：先向上对齐到 4 的倍数，至少 4，再除 2。引擎插件构造通过注入 storage / roles / resources，不是在前端文本框上更换一个 SQLite 路径。[构造](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/memory.py#L87)、[窗口换算](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/memory.py#L137)。

默认引擎还拥有独立工作区配置：数据库路径、top_k_history、score_threshold、relative_delta、procedure_guard_enabled、四类召回阈值及注入预算等。它们不是七栏“记忆基础”当前可见的全部输入框；后台配置存在不等于设置里展示。[默认引擎配置](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/default_memory/backend/config.py#L10)。

**Meido 对照**：已有记忆和向量启停、provider / URL / model / key / dimensions、测试连接、保存、状态反馈；维度当前上限 4096，语义记忆使用现有结构化引擎，尚无设置级通用引擎注册和替换。全局记忆 enabled 默认 true，和 Shiori 原始配置默认 false 不同；不能直接替换开关默认值导致已有角色行为改变。[Meido 记忆设置](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/MemorySettings.tsx#L8)、[持久默认](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/memory_config.py#L11)、[向量输入约束](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/models.py#L380)。

可测试闭环：关闭语义引擎仍保留历史 Markdown；恢复后仍可查到原有记忆；修改向量配置失败不能破坏现有可用连接；自定义引擎缺失应给明确错误，不能静默切默认并误认成功。若选择保持 Meido 的全局记忆暂停语义，页面必须标明差异，不复用含混同名说明。

### Meido 现有记忆开关迁移约束

6035dfd 的 memory_settings_store.get().enabled 同时被 MemoryService 写入 / 检索、MemoryOptimizerWorker / Loop、Markdown _role_memory_context、普通回合输入窗口整理、记忆工具授权及后台维护消费者读取。它当前确实是“全局暂停记忆”，不能只把 UI 改成 Shiori 的语义引擎开关，就让旧 disabled 用户重新启用 Markdown 注入或后台模型请求。[装配](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/main.py#L679)、[Markdown getter](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/main.py#L817)、[回合读取](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/main.py#L5911)、[后台维护](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/memory_service.py#L686)。

**迁移建议（计划）**：保留原 enabled=false 的明确暂停意图，新增语义引擎选择与独立后台整理开关时给每个消费者明确配置源；升级不得隐式开启旧用户的模型消费。可将“全局暂停记忆”保留为兼容总闸，显示 Shiori 风格基础字段时解释其作用；若决定拆成 Markdown / 语义引擎两个开关，必须给旧 false 一个全关闭迁移值，由主人显式恢复，不能按上游默认层始终开启硬套。仅 GET 或进入设置页不应触发消费。[当前恢复行为](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/main.py#L1442)。


## 四、外观栏：真实三个功能与 Meido 扩展

Shiori 的外观可见“聊天 / 实时显示回复”“界面 / 看板娘”“动效 / 背景呼吸与视差”。实时显示回复写 desktop.chat.streaming_enabled，通过普通设置事务作用于桌面输出。看板娘、背景动效即时写 renderer localStorage 的 version=1 偏好，两项默认 true；系统 prefers-reduced-motion 时动效显示关闭且锁定，保留用户原偏好以便系统恢复后使用。[外观组件](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/AppearanceSettingsSection.tsx#L15)、[偏好契约](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/shared/appearancePrefs.ts#L23)。

“看板娘”是 Shiori 的吟风引导、空状态、错误反馈及关于页角色，不等于可绑定角色并接收动作的桌宠窗口。Meido 6035dfd 已有本地流式显示偏好、字号、背景呼吸、减弱动态效果；但本地显示开关与 Shiori 后端 desktop 配置作用域不同，背景只有呼吸也不等于完整头像视差；尚未移植 Shiori 看板娘演出组件。[Meido 外观](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/AppearanceSettings.tsx#L5)、[Shiori 偏好说明](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/shared/appearancePrefs.ts#L13)。

可测试闭环：切换后立即看到实际输出或演出变化；重开 / 刷新保持；系统减弱动态变化实时锁定 / 恢复；关闭流式显示仍保存真实流事件且完成后一次展示，不改变模型的文本或工具结果。

## 五、高级栏：十三项、默认值、消费者、校验

以下 UI 清单全部来自 [advancedSettingsFields](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/advancedSettingsFields.ts#L20)；桌面默认来自 [settings.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settings.ts#L122)，校验来自 [validateSettings](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settings.ts#L227)。

| 分组 / 中文字段 | 持久键 | 桌面默认 | 实际消费者与复刻注意 |
| --- | --- | --- | --- |
| 对话 / 单次回复长度上限 | agent.max_tokens | 8192 | 传给模型 provider default_max_tokens 及主循环输出请求，同时作为输入预算输出预留。必须 > 0。[provider](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/providers.py#L43) |
| 对话 / 单次任务最多步数 | agent.max_iterations | 40 | 主推理循环限制模型步骤；0 表示不限（代码仅 > 0 时检查），不是“停用”。非负。纯 backend Config/load 缺配置时默认 10，与桌面读取默认 40 不同。[循环](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/core/passive_turn/reasoning_loop.py#L204)、[后端 load](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/config.py#L112) |
| 能力 / 联网搜索 | agent.tools.search_enabled | false | **名字具有误导性**：后端映射 tool_search_enabled，控制延迟 schema 的发现 / tool_search 模式；关闭后所有已获准工具 schema 可见，不是关掉 web_search 网络权限。[映射](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/config.py#L152)、[消费者](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/core/passive_turn/reasoner.py#L628) |
| 能力 / 创建子任务 | agent.tools.spawn_enabled | true | 决定 SpawnTool 和 SpawnManageTool 是否注册，后者含状态 / 取消等管理。Meido 复刻应允许关闭后仍清理旧任务，不能导致已有任务失控，也要覆盖 continue 的新执行授权。[注册](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/toolsets/meta.py#L39) |
| 记忆整理 / 记忆整理消息基数 | agent.context.memory_window | 40 | Markdown keep_count 的输入及消息窗口相关维护，不能直接当“上下文最大 token”。[构造和换算](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/memory.py#L97) |
| 记忆整理 / 后台整理记忆 | agent.maintenance.memory_optimizer_enabled | true | 控制 MemoryOptimizerLoop 是否创建；无模型注册时也不创建。[消费者](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/proactive.py#L215) |
| 记忆整理 / 整理间隔 | agent.maintenance.memory_optimizer_interval_seconds | 64800 秒（18 小时） | 注入 MemoryOptimizerLoop，循环会补跑过期角色并按间隔对齐；修改不能仅更新展示值。[构造](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/proactive.py#L234)、[循环](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/proactive_v2/memory_optimizer.py#L520) |
| 压缩 / 工作摘要长度上限 | agent.context.summary_token_limit | 2000 token | CompactionPolicy 使用，必须正整数，不是一般回复 max_tokens。[策略](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/compaction.py#L32) |
| 压缩 / 压缩后保留原文轮数 | agent.context.compaction_retained_turns | 2 | 同一 CompactionPolicy；非负整数，压缩保留完整轮而非任意消息条数。[策略](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/compaction.py#L32) |
| 压缩 / 上下文压缩触发比例 | agent.context.trigger_ratio | 0.75 | BudgetPolicy 和请求预算；和 target 联合满足 0 < target < trigger < 1。[预算](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/prompting/input_budget.py#L10) |
| 压缩 / 上下文压缩目标比例 | agent.context.target_ratio | 0.4 | 同上，压缩目标仍受硬输入上限限制；不能按单字段分别合法就接受 target ≥ trigger。[预算](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/prompting/input_budget.py#L73) |
| 压缩 / 输入安全余量 | agent.context.safety_margin_tokens | 4096 token | 每次请求从模型窗口扣除；非负整数，窗口扣输出和余量无空间则拒绝。[预算](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/prompting/input_budget.py#L73) |
| 开发者 / 开发者模式 | agent.dev_mode | false | provider 的 payload_snapshot_enabled，保存真实模型请求快照，不是单纯多显示日志。复制时需明确敏感 payload 的本地存储范围。[消费者](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/bootstrap/providers.py#L43) |

数字输入允许临时空值、0.、1e 等未完成文本，不向草稿提交 NaN；失焦恢复上次完整值。源码并非每一数字都有完善上下限或整数约束，例如旧普通表单 maxTokens 仅检查 >0、memoryWindow / optimizerInterval 没有在 validateSettings 全部限制；应复刻可用行为并补充目标端必要验证，不把上游漏校验当强制兼容。[数字输入](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/SettingsFieldPrimitives.tsx#L46)、[校验](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/settings.ts#L227)。

**Meido 对照**：已有 run_agent_loop.max_turns、MeidoProvider.max_response_tokens、按角色 Spawn 配额、DiscoveryState、记忆整理 loop，但没有上述十三项统一的全局已持久化入口。memory_optimizer_enabled 与 interval 目前从进程环境读取，MemoryMaintenance 的窗口仍是类常量；保存表单后要真实重配新运行和后台 loop。上下文预算与事务压缩并未因现有记忆整理而自动具备。[循环](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/agent_runtime/loop.py#L39)、[provider](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/agent_runtime/meido_provider.py#L161)、[环境读取](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/main.py#L688)、[记忆窗口常量](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/memory_maintenance.py#L23)。

可测试闭环：连续修改并重开十三项；新回合实际输出上限 / 步数变化；关闭子任务后新 spawn / continue 被拒，旧任务可查可停；背景整理开关和间隔修改后旧 loop 不重复运行；达到预算时自动摘要并保留指定完整轮；非法比例不写入；运行中改设置不把一次任务中途混入两套预算。

## 六、我的身份栏

Shiori 展示生成配对码、复制、剩余倒计时、重新生成、已绑定列表；列表包含头像或渠道图标、渠道名称、用户 ID、作用域（全平台 / 仅某账号）、绑定时间、解除绑定。解绑使用确认框并明确之后不再识别为主人。[配对面板](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/identities/PairingCodePanel.tsx#L15)、[列表](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/identities/IdentityList.tsx#L22)、[确认框](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/identities/IdentitySettingsSection.tsx#L36)。

配对码是 8 位去混淆字符，10 分钟、内存中唯一一个，重新生成替换、重启丢失、首次精确匹配消耗，大小写和首尾空白不敏感；身份刷新由 identities.updated 事件推送，并在新绑定到来时清空已消耗配对码。[后端](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/identity/pairing.py#L12)、[事件订阅](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/identities/useIdentities.ts#L12)。

**Meido 对照**：已有同类配对、倒计时、复制、绑定作用域、权限撤销和新绑定识别；当前仅在配对码活跃时每 2 秒轮询，解绑直接 DELETE，没有 Shiori 的确认框、头像、绑定日期和账号可读名称。因此属于“核心已存在、交互和信息缺口”，不需要重新设计整套渠道身份模型。[身份页](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/channels/IdentitySettings.tsx#L8)、[绑定列表](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/channels/IdentityBindings.tsx#L16)。

可测试闭环：生成 → 外部受控夹具消耗 → 页面立刻清码并显示作用域；重新生成旧码不可用；解绑先取消不发请求，确认后撤销身份和主人共享上下文；平台插件暂停仍可看已有绑定。真实平台事务另行按既有验收许可处理。

## 七、插件栏：动态包管理、配置 schema 和功能配置不是同一层

固定版本的源码目录有 23 个插件：11 个功能（browser_use、computer_use、desktop_pet、gpt_sovits_tts、herdr、meme、novelai、plugin_undo、screen_perception、sensevoice_asr、story），4 个渠道（feishu、qq、qqbot、telegram），8 个系统组件（citation、context_pressure、default_memory、observe、shell_restore、shell_safety、status_commands、tool_loop_guard）。运行目录还取决于安装 / 信任 / 冲突状态，不能把源码目录数直接当“全部可用”。[功能 manifest 示例](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/sensevoice_asr/manifest.yaml#L1)、[系统 manifest 示例](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/default_memory/manifest.yaml#L1)、[分组规则](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/pluginPresentation.ts#L19)。

### 宿主交互

- 功能 / 渠道 / 系统组件三组，空组隐藏，系统默认折叠；显示数量与异常数。名称打开详情，普通启用 / 关闭不额外挂 badge；未信任、激活中、需重启和失败分别显示有意义状态。[呈现](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/pluginPresentation.ts#L19)、[行](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/PluginRow.tsx#L39)。
- 只有已启用且有已注册设置子页的插件显示齿轮；关闭后隐藏其子页入口。外部插件有信任确认；插件详情可更新 / 卸载，顶部可选 ZIP 安装。安装不是把任意文件直接放进 Python import 路径。[目录控制器](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/PluginManagementSection.tsx#L52)、[包控制器](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/usePluginPackageController.ts#L1)。
- 最新版启停支持在当前 runtime generation 内 load / unload；配置更改也可独立热应用。只有不支持热卸载、包安装 / 更新 / 卸载等操作才保留待重启生命周期，**不是所有启停都必须重启**。[代内应用](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/apply.py#L143)、[待重启标签](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/pluginPresentation.ts#L57)。
- 配置源为插件自己的声明 schema；Pydantic 转 JSON Schema，标量 / 字符串数组使用专门控件，复杂结构折叠到高级 raw JSON；密钥识别有专门规则。每个插件拥有自己草稿和保存阶段。[schema](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/agent/plugin_host/config_schema.py#L1)、[表单](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/jsonSchemaForm.ts#L1)、[插件设置页](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/plugins/PluginSchemaSettingsSection.tsx#L25)。
- 配置读 committed TOML，不回传环境变量展开后的秘密，去掉宿主拥有的 enabled；保存独立目标插件表并做验证和运行应用，不能覆盖普通设置或其他插件。[读写](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/plugin_config.py#L39)、[隔离检查](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/apply.py#L68)。

### 主要插件子页字段与运行依赖

| 插件或子页 | 当前源码可见设置 / 边界 |
| --- | --- |
| Browser Use | headed 默认 false；操作超时默认 45 秒，范围 5–120。Meido 已有这组公共参数。[schema](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/browser_use/backend/config.py#L6) |
| Computer Use | 操作超时默认 30 秒，范围 5–120；Meido 当前公共参数页为“没有公共参数”，属于可补入口，但需对应实际 native 调用超时。[schema](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/computer_use/backend/config.py#L6) |
| Herdr | 路径、Session 动态列表、默认 Agent（Codex / Claude Code）、两种模型、CLI 超时 45 秒（5–300）、任务追踪超时 7200 秒（60–86400）；缺失 session 保留为“未找到”，不是静默换另一会话。Meido 既有 owned-mode、600 秒任务、仓库和 CLI 许可不能直接替成上游 FullAccess 方案。[表单](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/herdr/ui/Settings.tsx#L53)、[schema](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/herdr/backend/config.py#L11) |
| NovelAI | API Token、服务地址、默认 / NSFW 模型、NSFW 许可、文生图 / 图生图许可、素材自动写回、单张像素 / 采样步数上限、默认张数、质量标签、负面预设；角色生成设置另有自己的字段。Meido 已有独立 vault 和费用许可，默认模型不是消费许可。[schema](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/novelai/backend/config.py#L16) |
| 桌宠全局设置 | 实际为语音页：桌宠语音、热键、麦克风、识别 / 合成 provider 选择。动作包管理在角色素材库，不是同一全局语音页；每角色直播设置另存。[全局贡献](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/desktop_pet/ui/index.tsx#L15)、[语音字段](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/desktop_pet/ui/VoiceSettingsGroups.tsx#L27)、[直播配置](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/desktop_pet/backend/live_config.py#L27) |
| GPT-SoVITS | 外部服务 / 插件托管两模式；外部 URL、GPT / SoVITS 权重路径；配置版本、连接健康、模型身份未验证、推理状态恢复、托管环境包导入和启动停止。保存失败时阻止对旧配置误执行管理动作。[设置](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/gpt_sovits_tts/ui/Settings.tsx#L10)、[服务状态](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/gpt_sovits_tts/ui/ServiceStatusGroup.tsx#L21) |
| SenseVoiceSmall | 外部 / 托管、外部 URL、固定模型 / CPU 信息、检查连接、托管环境 ZIP 导入管理、录音识别测试。需要真实音频与进程宿主。[设置](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/sensevoice_asr/ui/Settings.tsx#L10) |
| 24h 视奸插件 | 名字如此，但 observe_screen 是按需主屏分析；未单独绑定视觉连接时用角色聊天连接，显式绑定失效不回退。Meido 6035dfd 已按 ADR-0023 实现此选择方向，不应重复称“仍强制配置视觉模型”。[选择](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/core/roles/model_runtime.py#L126)、[Meido ADR](../adr/0023-screen-vision-chat-fallback.md) |
| 工具循环保护 | repeat_limit 默认 3、最少 2，需要运行 hook 消费，不能只展示数字。[schema](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/tool_loop_guard/backend/config.py#L12) |
| Story | 不占七栏中的额外一栏；其独立全屏故事页内有文字速度、立即显示全文、动效强度、减少动态效果、语音 / 音乐 / 环境 / 界面音效四路音量。属于完整故事模式，不是缺一个普通全局表单。[独立入口](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/story/ui/index.tsx#L7)、[故事设置](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/story/ui/StorySettings.tsx#L13) |

渠道账号配置和角色用途配置仍由各贡献者负责，宿主总开关是全局；“取消每角色插件总开关”不等于删除每角色语音、CG、直播和模型绑定等有业务含义的参数。Shiori 角色级贡献可直接证明这种分离。[桌宠角色贡献](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/desktop_pet/ui/roleSettings.tsx#L1)、[NovelAI 角色贡献](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/plugins/novelai/ui/roleSettings.tsx#L1)。

**Meido 对照**：当前插件目录是 5 个宿主静态插件，加 UI 聚合的既有服务，不是动态发现的 23 个包。内部 PluginManifest 已有 schema / trust 信息，但设置没有通用 JSON Schema 配置页，也没有完整 ZIP 安装、信任、升级、卸载和启动恢复闭环；ASR / TTS / Story 更不能通过放入名字就算移植。[静态目录](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/plugin_management.py#L1)、[manifest 能力](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/backend/app/agent_runtime/plugins.py#L54)、[当前公共参数](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/PluginParameters.tsx#L7)。

可测试闭环：安装无害测试 ZIP → 展示真实 metadata 和信任边界 → 信任 / 启动 → schema 参数影响夹具工具 → 关闭撤销调用并完成清理；同 ID / 缺依赖 / 无效包不能加载；安装或更新失败保留旧版本；关闭一个插件不停止其他插件原生会话；普通设置并发保存不覆盖该插件参数。语音另需完整“导入环境 → 启动 → 健康 → 输入音频 → 识别 → 回复 → 合成 → 播放 → 停用清理”闭环。

## 八、关于栏：更新不是一个无功能按钮

可见版本卡、当前版本、检查更新 / 安装更新、更新日志；下载中有真实进度条，新版信息和失败详情；另有 GitHub / 联系作者卡和可关闭的看板娘。UI 通过受限 Electron preload 的 updates API，状态带 revision，旧 snapshot 不能盖掉新事件。[关于 UI](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/AboutSettingsPage.tsx#L36)、[订阅](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/renderer/src/settings/useDesktopUpdates.ts#L6)。

主进程只在 packaged 环境接入 electron-updater，并在启动检查；开发环境 unsupported。检查、下载、已就绪、安装中、失败分开；只能对完整已下载包 quitAndInstall(false,true)，安装失败能重试；不能在 Web 浏览器里凭一个按钮替换正在运行的 Python / Node 源码目录。[装配](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/updater.ts#L5)、[状态机](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/desktop/src/updateController.ts#L19)。

**Meido 对照**：6035dfd 的关于只有品牌说明和项目反馈链接，尚无主桌面应用版本 / 发布分发更新宿主；已有桌宠 Electron 是独立窗口宿主，不自动承担主应用发行更新。应先定义可更新产物、所有权和失败恢复，再实现真实 updater。[当前关于](https://github.com/sawaki-zexi/meido/blob/6035dfddeb7a9803d62f5a074d47569c59955723/web/src/features/settings/AboutSettings.tsx#L1)。

可测试闭环：开发态明确不支持；受控桌面安装包环境模拟可用更新 → 下载进度 → 校验完毕可安装 → 重启后版本变化，故障不丢用户数据。未经另行授权不执行实际安装或对外发布。

## 九、差距分类与交付顺序建议

以下是**计划建议**，不是已批准的新架构，也不代表新增功能已做好。

| 分类 | 项目 | 可交付用户结果 |
| --- | --- | --- |
| 已有功能但入口缺 / 可复用消费者 | 全局回复 token、主循环步数、后台整理启停与间隔、创建子任务总开关、Computer Use 超时、身份解绑确认和绑定信息 | 高级修改真正影响后续运行；关闭后保留管理与收尾；重开 / 重启读取一致 |
| 已有但语义不同 | search_enabled 实为 schema 延迟发现；记忆开关与引擎默认；流式显示的本地作用域；模型删除与默认连接 | 文案与真实行为一致；迁移保留原默认和绑定；针对拒绝 / 失败有明确反馈 |
| 必须新增运行能力 | 模型容量、按完整输入预算触发压缩、工作摘要与保留轮次、开发模式 payload 快照、通用记忆引擎注册、动态插件包生命周期 | 输入逼近窗口时真正压缩再发送；安装测试插件可用且可撤销，而非仅存配置 |
| 依赖桌面 / 原生产品闭环 | 主应用更新、看板娘演出、音频 ASR / TTS 托管环境与桌宠语音、Story 演出 | 必须有真实宿主、资源和端到端验收；不把无实现控件塞进设置制造“已支持”印象 |

建议先交付“高级全量 + 模型容量 / 压缩 + 保存失败恢复”的一个完整垂直闭环，再补记忆 / 身份 / 外观现有能力的等价行为，最后按独立规格推进动态包与音频 / 桌面发行。用户已经要求整体复刻，但仓库要求难以逆转或跨功能架构先确认；已有 D01–D03、ADR-0022 / 0023 等授权应复用，只对尚未覆盖的具体运行 / 发行方案提出确认。

## 十、版本差异与研究边界

将两个固定 Shiori 提交按 settings / settings.ts / desktop_bridge/runtime / renderer plugins 路径比较：七栏、模型容量、记忆 / 外观 / 高级十三项在两版一致；显著差异在插件独立配置热重载、代内启停与 renderer 同步，不能沿旧基线把所有插件更改都写成全应用重启。新基线的后端 [DerivedWrite.reload_plugin / toggle](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/apply.py#L143)、[代内应用路径](https://github.com/YinFengWindy/Shiori-Agent/blob/076a87b3b8df20140b9c4b683eeed2ff2a9db84a/apps/backend/desktop_bridge/runtime/apply.py#L378) 是本次结论依据。

本次以源码字段、持久化边界、调用消费者及测试中的协议为事实，不测试任何真实模型消费、平台发送或付费服务；未声称 23 个插件在当前 Windows 机器都可运行。本文完成的是调研和差距审计，实施与验收以随后规格、代码和实际测试为准。
