---
id: meme-manage
version: 1.0.0
description: 维护表情包库，添加图片、类别、描述、别名，整理 memes manifest。
tools: [read_file, list_dir, write_file, edit_file, shell]
activation: explicit
---
# 表情包维护

目录均相对当前角色 workspace：`memes/manifest.json` 与 `memes/<category>/001.png` 等真实图片。

1. 用 list_dir 检查 `memes`，read_file 阅读已有 manifest；不存在时再创建。
2. 核对主人提供的真实图片位置。read_file 读取图片应得到可见图像；缺文件或读取失败不能当有效素材。
3. 新建类别或调整描述、别名时，保留其他类别和现有图片。
4. 用 write_file/edit_file 更新 manifest，再 read_file/list_dir 核验内容和图片。

manifest 格式：

```json
{"version":1,"categories":{"happy":{"desc":"高兴、开心时使用","aliases":["开心"],"enabled":true}}}
```

类别名使用单个普通目录名，不能含 `..`、分隔符或绝对路径。
`desc` 说明情绪和场景；`aliases` 可空；`enabled:false` 暂停类别并保留文件。
write_file 只写 UTF-8 文本，不能把图片路径或 base64 字符串冒充二进制复制。
如图片已在获准 workspace，可使用主人已配置白名单的复制命令；Shell 未获准时说明需先准备图片。
渠道附件须先经已有附件授权取得真实文件，不采用模型自报路径，不读取另一角色媒体。
本技能维护素材库；发送图片仍需现有投递能力和相应许可。
完成时报告实际修改的类别、文件及核验结果；失败或依赖不足具体说明。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/meme-manage/SKILL.md（MIT）；按 Meido 文件与媒体边界改写。
