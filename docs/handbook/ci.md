# CI 检查范围与耗时

状态：当前

事实来源：[CI 工作流](../../.github/workflows/ci.yml)、[范围判定](../../.github/scripts/ci_scope.py)、[CI 依赖](../../requirements-ci.txt)。

最后核验：2026-10-18

## 何时执行

- 面向 `main` 的 PR 执行一套 CI；功能分支的 push 不单独触发，推送更新会触发该 PR 的检查。
- `main` 的 push 执行完整前后端检查，包含直接推送与合并提交。
- 新提交取消同一来源、同一分支、同一事件的过期运行；不同 PR 互不取消。

## PR 检查范围

- `web/`、`desktop-pet/` 及根目录 Node、TypeScript、Vite、Vitest 配置变化：前端类型检查、网页和桌宠构建、前端测试。
- `backend/`、`tests/python/`、`requirements.txt` 变化：Python 编译检查和全部后端测试。
- 仅修改 `docs/`、根目录 `README.md`、`CONTEXT.md`、`AGENTS.md`、`LICENSE`：跳过前后端任务。
- 其他路径（含工作流、CI 依赖、脚本和新增配置）或跨前后端变化：执行对应范围的并集；未知路径保守执行全部检查。
- 删除和重命名同样参与判定；重命名按旧路径删除与新路径新增处理。范围判定失败时不允许静默跳过检查。

工作流本身始终启动，保留 `Frontend build` 与 `Backend tests` 检查名称；不使用工作流级路径过滤，以免必需检查一直等待。

## 后端并行与本地复现

CI 安装 `requirements-ci.txt`，使用两个独立进程按文件调度；同一文件的测试留在同一进程。测试失败照常失败，worker 崩溃不自动重启，也不重试测试。

本地复现时，先创建并激活独立虚拟环境，再安装下面的依赖。不要只通过 `--system-site-packages` 借用系统依赖：应用重建测试会隔离子进程，并且只显式加载当前虚拟环境的依赖目录；借用的系统包在子进程中不可用。

```sh
python -m pip install -r requirements-ci.txt
python -m pytest -n 2 --dist loadfile --max-worker-restart=0 -o faulthandler_timeout=90 --durations=15
```

本地开发仍可用 `python -m pytest` 串行执行。`--durations=15` 在 CI 日志中列出慢测试，后续优化以真实耗时为依据。

Windows 下应使用较短的工作区路径。若默认用户临时目录过长，可在测试命令后添加 `--basetemp=.pytest_cache/w`，将 pytest 临时文件放到仓库内的专用短目录，避免任务日志的嵌套路径超出 Windows 路径长度限制。该目录仅供 pytest 管理临时数据。
