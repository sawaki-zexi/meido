# CI 与发布

状态：当前

事实来源：[CI 工作流](../../.github/workflows/ci.yml)、[Pages 工作流](../../.github/workflows/pages.yml)。
最后核验：2026-10-29。

## 检查

面向 `main` 的 PR 和 `main` 的 push 均执行完整 CI；新提交取消同一 PR 或分支的旧运行。
不按改动路径跳过检查，纯文档修改也执行测试。

分支保护要求以下四个检查，工作流直接运行并报告结果：

- `Frontend build`：TypeScript 检查、网页与桌宠构建、前端测试、以 `/meido/` 为基路径的独立落地页构建。
- `Backend tests (core)`：`tests/python` 中除渠道和 Drift 素材 API 之外的测试。
- `Backend tests (channels)`：`tests/python/channels`。
- `Backend tests (drift)`：`tests/python/test_drift_meme_api.py`。

后端三组并行，组内串行，不重试失败。结果和慢测试耗时直接查看 Actions 日志。
PR 的 Checks 页面显示检查结果，合并条件由 GitHub 分支保护执行。

## 本地复现

安装 Node 22 和 Python 3.12，在独立工作区与虚拟环境中运行：

```sh
npm ci
npm run build
npm run test:web
npm run build:landing -- --base /meido/
python -m pip install -r requirements.txt
python -m pytest tests/python -o faulthandler_timeout=90 --durations=15
```

Windows 临时路径过长时，可在 pytest 命令后添加 `--basetemp=.pytest_cache/w`。
该目录仅供 pytest 管理临时数据。

## 发布

PR 的落地页测试和构建由前端 CI 完成。落地页相关变更进入 `main` 后，
`Landing Pages` 构建并发布静态页面；也可手动选择 `main` 重新发布。
具体步骤见[落地页发布](landing-publication.md)。
