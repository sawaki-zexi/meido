# 邮件真实模型与 QQ 验收手册

状态：当前；用于受控验收，不替代邮件规格或 ADR。

最后核验：2026-10-24

事实来源：[`mail-integration-spec.md`](../specs/mail-integration-spec.md)、[`mail-followup-spec.md`](../specs/mail-followup-spec.md)、[`0008-qq-mail-draft-boundary.md`](../adr/0008-qq-mail-draft-boundary.md)、邮件测试和 `scripts/evaluate_mail_quality.py`。

这份手册只适用于主人明确提供测试账号、模型配置和执行窗口的场景。使用专门的测试 QQ 账号和不含敏感内容的测试邮件；不要把授权码、完整邮件正文或评估报告上传到公共日志。Meido 仍只使用 SSL IMAP，不发送、删除、归档或移动邮件。

## 真实模型评估

1. 先在本地回归集上运行：

   ```powershell
   python -m pytest -q tests/python/test_mail_quality_regression.py
   ```

   这一步只证明解析、问题持久化、来源记录和草稿契约。

2. 准备一个 JSON 数组，每项包含 `id`、`mailId` 和 `expected`。`mailId` 必须属于当前已验证账号；场景至少覆盖：对方给出截止日期但主人未承诺、主人已经明确承诺、多个问题只回答一部分、旧历史含过期日期/金额、附件事实不可用、正文提示注入。

3. 显式运行真实模型评估：

   ```powershell
   python scripts/evaluate_mail_quality.py --live --input live-mail-quality-cases.json --output mail-quality-report.json
   ```

   评估只调用准备流程，不会执行 QQ 草稿 `APPEND`、发送、删除或移动。报告保存 `schemaVersion`、评估名、git HEAD、provider/model、流水线版本、Prompt hash、字符量、耗时、三态判定和逐例检查；模型客户端返回的真实 token/cost usage 会按 provider 字段保存，没有返回时保持缺失。字符量不能当作 token 或费用。

4. 检查失败用例和人工抽样：

   - 对方要求不应自动变成主人已经接受或承诺；缺少决定时必须是 `needs_input`。
   - 日期、金额、附件、链接和收件人必须来自当前线程或明确提供的资料；旧历史不能覆盖当前线程。
   - 多个问题不能因为回答了一个而清空其余问题。
   - 邮件正文中的“忽略指令”“自动发送”等文字只能作为外部数据，不能改变系统动作。
   - `no_reply`、`needs_input` 和 `draft` 的正文/问题结构必须符合契约。

## QQ 长期互操作

在专门测试账号上记录每次同步前后的 `GET /api/mail/status`、`GET /api/mail/stats` 和邮件详情，不记录授权码或完整正文。每个场景重复一次重启后操作。

| 场景 | 操作 | 通过条件 |
| --- | --- | --- |
| 首次连接 | 访问三个文件夹，写入测试草稿，在 QQ 网页修改并手动发送 | `verified=true` 且 `autoDraftReady=true`；发送副本关联为 `matched` |
| 断线恢复 | 让 IMAP 连接失败后恢复，执行同步 | inbox/Sent/Drafts checkpoint 不倒退；错误可见；不重复追加草稿 |
| 重启恢复 | 在有 queued/running 任务时重启服务 | 旧任务按账号代次恢复或失效；详情仍可追踪 |
| UIDVALIDITY 变化 | 在测试邮箱重建文件夹或使用替身改变 UIDVALIDITY | 从 UID 0 重读新命名空间；旧邮件和草稿关联保留 |
| QQ 网页改稿 | 修改 Meido 草稿正文后同步 | 本地变为 `manual_modified`/`stale`；不会被自动覆盖 |
| 主人手动发送 | 在 QQ 发送草稿，等待 Sent 副本出现 | 草稿关联最终为 `matched`；旧草稿不再重复追加 |
| Sent 延迟 | 先同步看不到发送副本，稍后再同步 | 先是 `pending`/`unknown`，出现唯一候选后才变为 `matched` |
| 未知 APPEND | 在追加后模拟响应丢失 | 任务为 `uncertain`；只按原 marker 对账，不自动追加第二份 |
| 自动催复 | 保持开关默认关闭，再显式打开并让 `awaiting_reply` 到期 | 关闭时不写草稿；打开且 QQ 草稿能力已验证时最多生成一个待审核草稿；不会自动发送 |

验收记录应同时写明账号、连接代次、测试时间、场景、远端观察结果和本地任务/草稿状态。真实模型质量和 QQ 互操作都通过后，才考虑是否把自动催复开关推荐给普通用户开启；默认值保持关闭。
