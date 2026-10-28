# NovelAI 生图响应兼容性核验（#263）

状态：当前；最后核验：2026-10-28。
事实来源：Meido `bb29bdd` 的 `novelai_generation/http.py`、`media.py`；Shiori `d3acaed186b172a78a5892e0307e9affd89759f0` 的 `plugins/novelai/backend/client.py`、`service.py`；[NovelAI 官方接口](https://image.novelai.net/docs/doc.json)；`tests/python/test_novelai_http.py`、`test_novelai_limits.py`。

## 已提取

- 用户两张截图显示工作台本地就绪、生成结果不确定，展开信息仍是通用错误；实际正向 tags 为 `1`。截图未显示内部 `errorCode`、响应头或响应体，不能据此认定唯一远端原因。
- 请求地址、Bearer 认证、V4.5 请求参数与 Shiori 一致。Meido 原传输层却只接受精确 MIME 和 identity 编码；Shiori 会按 ZIP 签名解析二进制包，并接受 HTTP 客户端已解码的响应。
- Meido 仅接受 `images` 中的 `{image,index,seed}` 对象，符合核验时的官方 JSON schema；Shiori 的现有解析器接受 `images` 中的 base64 字符串。两者需要兼容，而非删除官方对象形式。
- 实际 loopback HTTP、公开手动 API 和角色工具复现：有效的二进制 MIME ZIP、无 MIME ZIP、gzip/deflate ZIP、Shiori 字符串 JSON 在修复前均产生 `unknown` 且无图片。前两类 `invalid_response_type` 及压缩类型 `unsupported_content_encoding` 没有专用用户解释，能产生截图中的通用提示；这不是对用户真实远端响应的证明。

## 实现与限制

接受上述响应形式，但仍验证签名、完整图片、像素/条目/字节限额。HTTP 压缩体与解压后的响应均受 32MiB 上限约束，ZIP 内解压仍为 64MiB。没有改动身份、权限、预算、一次 POST、禁重试/重定向及环境代理边界。旧未知 attempt 不重发；新请求使用修复后的解析。

补齐安全错误说明及可见错误代码/尝试编号。无附件时只显示记录和未保存图片的事实，不再误报“图片保留在历史”。供应商响应正文、token、输入图片均不回显。

仅使用隔离无费用夹具；未调用真实 NovelAI 账号，未确认真实扣费或截图对应的唯一故障原因。固定 30 秒网络读超时与 Shiori 默认保持一致，本轮未将它视为已证实根因。

## 本机只读诊断补充

用户随后连接本机，运行版本确认为 `bb29bdd`。通过只读 API 与对应 SQLite 记录交叉核验，截图所示请求的实际错误为 `invalid_response_type`：HTTP 已通过 200/201 检查，在响应 Content-Type 白名单处退出，尚未读取响应体；无保存图片或附件。因此本次兼容修复覆盖实际失败代码路径，错误码展示和无附件文案修复也直接适用。

旧记录未保存原始 MIME 或响应体，不能由此断言远端一定返回合法 ZIP，亦未重新调用收费接口或确认真实计费。当前角色表情素材接口正常返回空列表，磁盘对应库也是空的；未在用户现有空库复现预览故障，重启预览修复由隔离流程证明。无用户密钥、原始聊天、设备路径或具体记录标识写入本报告。
