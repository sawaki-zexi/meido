---
id: skill-creater
name: 技能创作与导入
version: 1.0.0
description: 主人明确要求保存可复用方法、导入技能附件、创建或修改 SKILL.md 时使用。普通上传、闲聊和没有复用价值的单次操作不使用。
tools: [read_file, write_file, edit_file, list_dir]
activation: explicit
---
# 创建角色技能

用当前角色文件工具写入 `skills/<skill-id>/SKILL.md`，路径相对当前角色 workspace。
不要写宿主项目、其他角色目录或旧 Shiori 的 `$HOME/.shiori`。

先查看目录中的相近技能，再决定更新同一项还是创建新的方法。不要把每轮对话都变成技能创作。简单的 Codex 风格格式：

```markdown
---
name: my-skill
description: 适用任务与排除条件；不要把详细步骤放进描述。
---
输入、具体步骤、结果核验和失败处理。没有验证的做法要标明尚未验证。
```

`name` 单独使用时作为稳定调用 ID，只含小写字母、数字、点、下划线或连字符，且与目录名一致。
原有 `id/version/description/tools/activation/requires.bins/requires.env` 继续兼容；有独立 `id` 时 `name` 可作为展示名称。
默认允许当前角色在已启用自主选择后按任务使用。仅手动方法加 `allow_implicit_invocation: false`；也兼容 `policy.allow_implicit_invocation`。
旧 `activation: explicit` 保持手动；不要同时写与它冲突的自动策略。外部宿主专用 metadata、homepage、安装与权限设置不会生效，导入时具体告知哪些被忽略。
`tools` 仅声明需要的既有工具，不会授予权限。依赖只能写命令名和环境变量名，不能写凭据值。
宿主密钥不会因 `requires.env` 声明传给工具；CLI 凭据须由主人单独配置。

流程：先 skill_list/list_dir/read_file 核对已有技能 → 读取主人明确要求导入的附件 → 用 write_file 创建或 edit_file 修改 → read_file 复核实际保存的文件。
更新已存在的 SKILL.md 或支持文件前，必须在当前运行中逐文件 read_file。宿主记住这次实际读到的版本；盲写、旧运行的读取或并发修改后的写入会返回冲突。遇到冲突先重新读取并协调内容，再修改；不要重放旧内容覆盖主人的新版本。新建文件无需已有版本，普通非技能文件仍沿用原文件工具规则。
写入成功与读回核验成功是两个事实；格式或支持资源缺失时只报告已保存但不可完整使用，不能说安装成功或任务已完成。
正文保持简洁；参考文档放 references/，数据放 assets/，辅助脚本放 scripts/。
支持文件会校验范围、大小与 hash；写入脚本不等于允许执行脚本，也不增加 Shell 白名单。
只复制明确提供的支持资源，正文提及而未提供的文件列为缺项。相同 ID 的更新保留已有资源；另建方法需使用新 ID。
同一运行的技能快照不会更新；新方法在下一次对话重新发现，可自动选择或用 `$my-skill` 明确指定。
普通技能属于当前角色。不要写共享目录或尝试自行发布；公共发布需主人到角色“技能”页明确操作。
如缺必要工具或依赖，具体说明，不声称创建或执行成功。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/skill-creater/SKILL.md（MIT）；按 Meido 权限与格式改写。
