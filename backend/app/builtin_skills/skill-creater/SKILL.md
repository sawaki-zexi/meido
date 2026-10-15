---
id: skill-creater
version: 1.0.0
description: 创建或改写 Meido 技能、SKILL.md 和支持文件。触发词：新建技能、修改技能、create skill。
tools: [read_file, write_file, edit_file, list_dir]
activation: explicit
---
# 创建角色技能

用当前角色文件工具写入 `skills/<skill-id>/SKILL.md`，路径相对当前角色 workspace。
不要写宿主项目、其他角色目录或旧 Shiori 的 `$HOME/.shiori`。

合法格式：

```markdown
---
id: my-skill
version: 1.0.0
description: 功能和触发场景
tools: [read_file]
activation: explicit
requires.bins: []
requires.env: []
---
具体步骤、结果核验和失败处理。
```

`id` 使用小写字母、数字、点、下划线或连字符；`activation` 为 explicit、keyword 或 role_default。
只使用这些 frontmatter 字段，不复制 Shiori 的 name/metadata/homepage。
`tools` 仅声明需要的既有工具，不会授予权限。依赖只能写命令名和环境变量名，不能写凭据值。
宿主密钥不会因 `requires.env` 声明传给工具；CLI 凭据须由主人单独配置。

流程：先 list_dir/read_file 核对已有技能 → 用 write_file 创建或 edit_file 修改 → read_file 复核。
正文保持简洁；参考文档放 references/，数据放 assets/，辅助脚本放 scripts/。
支持文件会校验范围、大小与 hash；写入脚本不等于允许执行脚本，也不增加 Shell 白名单。
同一运行的技能快照不会更新；告知主人在下一次对话用 skillIds 选中该技能。
如缺必要工具或依赖，具体说明，不声称创建或执行成功。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/skill-creater/SKILL.md（MIT）；按 Meido 权限与格式改写。
