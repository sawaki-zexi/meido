# CI 与发布

状态：当前

事实来源：[CI 工作流](../../.github/workflows/ci.yml)、[Pages 工作流](../../.github/workflows/pages.yml)。
最后核验：2026-11-15。

## 检查

面向 `main` 的 PR 和 `main` 的 push 均执行前端检查与后端烟测；新提交取消同一 PR 或分支的旧运行。
不按改动路径跳过检查，纯文档修改也执行测试。

当前工作流运行以下检查；实际合并门禁以远端分支保护为准：

- `Frontend build`：TypeScript 检查、网页与桌宠构建、前端测试、以 `/meido/` 为基路径的独立落地页构建。
- `Backend tests (core)`：角色保存/编辑/删除、模型配置、聊天流与持久化、工具调用的基本流程。
- `Backend tests (channels)`：QQ 消息解析、发送与渠道回复提交的基本流程。
- `Backend tests (drift)`：表情维护配置、素材管理、持久说明、原件收藏与发送、来源撤权及符号链接防护。
- `Backend tests (chat-lifecycle)`：聊天、后台任务和运行资源生命周期。
- `Backend tests (context-preparation)`：上下文、记忆、能力与工具暴露。
- `Windows chat E2E`：独立 Windows 服务启动器和聊天端到端验收。

后端运行工作流中明确列出的相关回归，五组并行；channels 限时 10 分钟，其他组 5 分钟，不重试失败。
素材组保留全部符号链接安全用例并在 Linux 执行。旧自然插画单阶段模型用例 `test_natural_qq_uses_enabled_role_illustrations_without_tools` 在基线 `b2e8106` 也失败，暂未加入该组；当前自然候选范围由 `test_material_message_scope.py` 覆盖，旧用例未删除或放宽断言。
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
