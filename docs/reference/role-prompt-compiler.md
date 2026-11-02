# 角色提示词编译

状态：当前。最后核验：2026-11-02。

事实来源：[编译器](../../backend/app/role_prompt.py)、[请求回归](../../tests/python/test_role_prompt_compiler.py)、
[Provider](../../backend/app/agent_runtime/meido_provider.py)及[提醒入口](../../backend/app/main.py)。

`compile_role_prompt(role)` 是纯函数，返回 `text` 和不可变的 `diagnostics`。
只读取角色名称、简介及现有五个设定字段，按身份、简介、角色设定、性格、
行为规则、回复约束、称呼顺序输出。沿用现有中文标签；字段两端空白去除，
内部换行保留，空字段不生成标签。相同输入得到相同文本和诊断，不修改角色。

## 宏规则

- `{{char}}` 使用角色名称；`{{user}}` 使用 `profile.nickname`，空称呼回退为“用户”。
- 名称大小写不敏感，允许花括号内的前后空白，例如 `{{ USER }}`。导入预览使用
  同一支持判断，不再把这些空白形式误报为未知宏。
- 对每个原始字段仅替换一次。替换值中的花括号保留，不递归解释，不执行表达式。
- 未知宏原样保留；每个字段内相同的未知宏只产生一条 `{field, macro}` 诊断，
  按字段和首次出现顺序返回。诊断供调用方检查，没有新增日志、数据库或界面。
- Meido 的 `nickname` 表示“称呼用户”，不是 Shiori 的角色别名。宏替换不证明
  渠道发送者身份，也不授予主人权限。

## 运行边界

普通对话、工具续轮、桌面及渠道的 MeidoProvider，自带 OpenAI-compatible 与
路由适配器的 `stream_reply`，以及待办提醒都消费相同角色编译文本。
记忆、主人资料、能力和当前来源仍由 Runtime 在角色文本之后单独装配；动态
context frame 的位置和授权合同不变。这些运行时材料、用户输入、工具结果和
待办事实不会经过角色宏替换。计量和发送继续共享原有准备请求。

第三方旧 `stream_reply(role, history, configuration)` 接口的上下文兼容副本保持
原合同；本次不替第三方重写内部提示词，不能保证任意外部旧适配器支持上述宏。
辅助记忆、生图资料和素材导出不属于角色扮演编译入口。

角色卡来源、作者备注、扩展对象、模型凭据、能力授权和工作区状态不参与编译。
没有增加设定版本化、持久化迁移、角色预览 UI 或磁盘旧凭据清理。

## 参考与适配

行为参考固定 Shiori [`role_prompt_compiler.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/backend/core/roles/role_prompt_compiler.py)
和 [`role_macros.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/backend/core/roles/role_macros.py)，
以及 Pi [`system-prompt.ts`](https://github.com/earendil-works/pi/blob/ddbe02cd2014bedc9555b7d782da90a2fd5e8abc/packages/coding-agent/src/core/system-prompt.ts)
的确定构建边界。此实现按 Meido 的现有五字段独立编写，没有迁入其角色结构、
昵称语义或消息协议。
