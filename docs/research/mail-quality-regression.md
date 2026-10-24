# 邮件回复质量回归夹具

状态：当前；用于比较邮件上下文、提示词和模型结果契约的离线回归，不代表真实模型质量或真实 QQ 互操作已经验收。

事实来源：`backend/app/mail/{evaluation,preparation,service}.py`、`scripts/evaluate_mail_quality.py`、`tests/python/test_mail_quality_regression.py`、`tests/fixtures/mail_quality_cases.json`，以及 Inbox Zero 固定提交 `aea0ec2db43a1147b90776d9bbf3f467afc0af26` 的评估测试。

最后核验：2026-10-24

## 目的

`tests/fixtures/mail_quality_cases.json` 固定八类代表场景：

- 缺少主人决定时必须返回 `needs_input`，不能替主人选择；
- 系统通知可以返回 `no_reply`；
- 对方给出截止日期但主人尚未承诺时仍需补充决定；
- 已有主人明确承诺时可以复述日期，但不能引入旧金额或新日期；
- 多个问题必须逐项保留；
- 附件事实缺失和邮件正文提示注入不能被当作事实或执行指令；
- 写作偏好和同对象历史回复会进入同一次起草上下文。

测试使用真实 `MailService`、SQLite、来源记录和草稿版本逻辑，只有 IMAP 和模型调用替换为确定性夹具。它会检查三态、草稿数量、问题持久化、禁止/要求的文本和上下文注入。

运行：

```powershell
python -m pytest -q tests/python/test_mail_quality_regression.py
```

需要比较真实模型时，保留同一组输入和断言，另外记录模型、配置、流水线版本、Prompt 哈希、耗时及人工抽检结果；不能把夹具模型的通过结果写成真实模型评测。

## 评估入口

`backend/app/mail/evaluation.py` 只评估已经解析的结构化准备结果。它检查决定、正文、问题和显式的必需/禁止文本，并记录 provider、model、配置 ID、流水线版本、Prompt hash、输入输出字符数、模型耗时、可选的 provider token/cost usage 和人工复核字段。生产邮件模型入口会优先保留 rich response 的 provider usage；回放或旧 callback 没有 usage 时字段保持 `null`，不会用字符数冒充 token 或费用。来源清单、confidence 和 hash 只用于归因，不会被当作事实核验。

需要独立语义评审时，调用方可以把一个 `judge(case, preparation)` 函数传给 `evaluate_preparation`。评审器返回 `{status: "pass" | "fail" | "error", score?: 0..1, checks?: [{name, passed, detail}], notes?: string}`；评审结果会被规范化并写入 `judgeStatus`、`judgeScore`、`judgeChecks` 和 `judgeNotes`，任何异常或非法分数都会按失败处理。评估器本身不选择模型、不自动联网，也不会把没有运行 judge 的结果当作通过；这样可以接入独立模型或人工审核，同时保持回放模式无副作用。

回放评估不会调用模型：

```powershell
python scripts/evaluate_mail_quality.py --input recorded-cases.json --output report.json
```

仓库内的固定夹具也可以直接回放；它把确定性模型响应放在 `model` 字段，脚本会将其作为记录的 preparation 处理：

```powershell
python scripts/evaluate_mail_quality.py --input tests/fixtures/mail_quality_cases.json
```

`recorded-cases.json` 的每项包含 `id`、`expected` 和 `preparation`。对已经同步到当前 Meido 邮箱的邮件，可以显式使用 `--live`，调用实际模型的上下文准备函数；这个模式只读取邮件和模型输出，不执行 QQ 草稿追加、发送、删除或移动：

```powershell
python scripts/evaluate_mail_quality.py --live --input live-cases.json --output report.json
```

实时用例还需要提供 `mailId`。运行前应固定输入邮件、模型配置和评估标签，并在报告的 `reviewerStatus`/`reviewerNotes` 中完成人工抽检。报告还记录 `schemaVersion`、评估输入名、git HEAD 和实际字符/耗时用量；没有 provider token/cost 时不会用字符数冒充 token 或费用。报告通过不代表模型事实正确。

如果需要独立的人工或第二个模型语义评审，可以把结果保存为以用例 ID 为键的 JSON，再显式传入：

```powershell
python scripts/evaluate_mail_quality.py --live --input live-cases.json --judge-report judge.json --output report.json
```

文件格式可以是 `{ "case-id": { "status": "pass", "score": 0.9, "checks": [], "notes": "..." } }`，也可以包在 `{ "judgements": { ... } }` 中。评审文件缺少某个用例、状态非法或检查项格式错误时，该用例按失败处理；评估器不会自行调用第二个模型，也不会把来源元数据当成语义事实证明。
