# CI 与发布

状态：当前

事实来源：[CI 工作流](../../.github/workflows/ci.yml)、[Pages 工作流](../../.github/workflows/pages.yml)。
最后核验：2026-10-29。

## 检查

面向 `main` 的 PR 和 `main` 的 push 均执行前端检查与后端烟测；新提交取消同一 PR 或分支的旧运行。
不按改动路径跳过检查，纯文档修改也执行测试。

分支保护要求以下四个检查，工作流直接运行并报告结果：

- `Frontend build`：TypeScript 检查、网页与桌宠构建、前端测试、以 `/meido/` 为基路径的独立落地页构建。
- `Backend tests (core)`：角色保存/编辑/删除、模型配置、聊天流与持久化、工具调用的基本流程。
- `Backend tests (channels)`：QQ 消息解析、发送与渠道回复提交的基本流程。
- `Backend tests (drift)`：启用内置表情库维护与读取配置。

后端只运行工作流中列出的 13 项烟测，三组并行，每组限时 5 分钟，不重试失败。
保留三个名称是为了兼容现有分支保护，每组都执行真实测试。
结果和耗时直接查看 Actions 日志。
PR 的 Checks 页面显示检查结果，合并条件由 GitHub 分支保护执行。

## 按需回归

日常后端 CI 不覆盖全部功能、故障恢复、竞态和边界组合。
开发时按改动范围运行相关测试；现有完整测试集保留，需要时在本地运行。

主分支运行 `40535683833` 的全量 core 测试耗时 17 分 27 秒，
因此日常 CI 改为只检查基本可用性；烟测通过不代表完整后端回归通过。

## 本地验证

安装 Node 22 和 Python 3.12，在独立工作区与虚拟环境中运行：

```sh
npm ci
npm run build
npm run test:web
npm run build:landing -- --base /meido/
python -m pip install -r requirements.txt
python -m pytest tests/python -o faulthandler_timeout=90 --durations=15
```

Windows 临时路径过长时，先创建 `.pytest_cache` 目录，再在 pytest 命令后添加 `--basetemp=.pytest_cache/w`。
该目录仅供 pytest 管理临时数据。

## 发布

PR 的落地页测试和构建由前端 CI 完成。落地页相关变更进入 `main` 后，
`Landing Pages` 构建并发布静态页面；也可手动选择 `main` 重新发布。
具体步骤见[落地页发布](landing-publication.md)。
