---
id: create-drift-skill
version: 1.0.0
description: 在当前角色的 Drift 专用目录创建或更新可被后续空闲运行执行的长期小任务和工作文件。
tools: [read_file, list_dir, write_file, edit_file]
activation: explicit
---
# 创建长期 Drift 技能

将适合分多次推进的长期小任务保存为当前角色的真实文件。普通聊天中的本技能负责作者工作；Drift 开关、模型、工具、MCP、只读资源和发送许可仍由主人单独配置。产物不会自行取得权限。

1. 用 list_dir/read_file 检查 `drift/skills/<id>/SKILL.md` 和 `drift/work/<id>`；更新已有任务时保留原工作文件与进度。
2. 确认任务有一个有限、可重复的小步骤。只使用小写字母、数字、点、下划线或连字符的合法 id；下面三个模板中的 id 和路径必须一起改。
3. 先写 `references/drift.json` 和工作文件，最后写 SKILL.md。SKILL header 使用 Meido 的 id/version/description/tools/activation，不把 requiresMCP 写入 header。
4. 依赖写在 `references/drift.json`：只声明已有或等待主人配置的 serverId、所需远端工具名，以及可选的精确 definitionHash。Shell 只声明宿主提供的规则 ID。不要写 command/env/凭据/安装指令，不自己计算 SHA 或写 trust=true。宿主登记实际文件 hash、作者来源与待补依赖，缺依赖不表示生成失败。
5. 用 read_file 核验保存结果。作者当下只有 FileTools 不妨碍声明后续获准的 MCP/Shell；执行须再满足作者声明范围和后续运行的精确许可。报告真实保存文件、依赖状态和下一步，不能把保存说成已运行或已发送。

以下三份小模板可直接通过 write_file 写入，都是当前角色 workspace 的相对路径。工作文件放在 work 下，不改变技能不可变支持文件 hash；宿主 state 目录不能由模型写入。

若宿主明确以 Drift 自主身份激活本技能，文件工具的根已经是 Drift 专用根：下列三个路径及检查路径都须去掉最前面的 `drift/`，使用 `skills/...` 和 `work/...`。普通主人聊天保留 `drift/`。宿主决定当前身份和根，文本不能自行切换身份或目录。

### `drift/skills/reading-notes/references/drift.json`
```json
{"profile":"meido-drift-skill-v1","version":1,"skillId":"reading-notes","requiresMCP":[],"readResourceIds":[],"shellRuleIds":[]}
```

### `drift/work/reading-notes/notes.md`
```text
读书笔记长期任务
当前进度：尚未开始。
下一步：阅读并写下一条简短、可核验的笔记。
```

### `drift/skills/reading-notes/SKILL.md`
```markdown
---
id: reading-notes
version: 1.0.0
description: 每次空闲运行推进一条读书笔记，保留工作文件与下一步。
tools: [read_file, write_file, edit_file, finish_drift]
activation: explicit
---
# 读书笔记
读取 work/reading-notes/notes.md 和宿主提供的上次 next_action；只在本角色 Drift 根内推进一个有限步骤，再保存工作文件。
没有获准素材时如实记下缺项，不能捏造已经读过的书。
默认静默，用 finish_drift(skill_used="reading-notes", one_line="本次实际进展", next_action="下一次有限动作", message_result="silent") 结束。
仅主人已授予发送许可且有实际需要时才 message_push；成功 receipt 后只能 write_file/edit_file/finish_drift，finish 的 message_result 必须是 sent。
若实际唯一发送确定失败则 finish failed；结果等待确认或不明则 finish uncertain。无 finish、取消或预算耗尽不冒充完成。
```

不要写 builtin 目录、其他角色、绝对路径或宿主 state 文件。再次运行使用同一技能 id 和 work 文件；只有流程/依赖变化才更新 SKILL/支持文件，新的 hash 由宿主重新校验。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/create-drift-skill/SKILL.md（MIT）；按 Meido 专用作者范围、真实依赖、持久进度和四态 finish 改写。
