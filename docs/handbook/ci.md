# CI 检查范围与耗时

状态：当前

事实来源：[CI 工作流](../../.github/workflows/ci.yml)、[范围判定](../../.github/scripts/ci_scope.py)、[工作流回归检查](../../.github/tests/test_ci_workflow.py)、[必需检查核对工具](../../.github/scripts/required_checks.py)。

最后核验：2026-10-24

## 何时执行

- 面向 `main` 的 PR 执行一套 CI；功能分支的 push 不单独触发，推送更新会触发该 PR 的检查。
- `main` 的 push 执行完整前后端检查，包含直接推送与合并提交。
- 新提交取消同一来源、同一分支、同一事件的过期运行；不同 PR 互不取消。

## PR 检查范围

- `web/`、`desktop-pet/` 及根目录 Node、TypeScript、Vite、Vitest 配置变化：前端类型检查、网页和桌宠构建、前端测试。
- `backend/`、`tests/python/`、`requirements.txt` 变化：Python 编译检查和全部后端测试。
- 仅修改 `docs/`、根目录 `README.md`、`CONTEXT.md`、`AGENTS.md`、`LICENSE`：跳过前端任务和后端测试步骤，三个后端检查仍以稳定名称完成。
- 其他路径（含工作流、CI 依赖、脚本和新增配置）或跨前后端变化：执行对应范围的并集；未知路径保守执行全部检查。
- 删除和重命名同样参与判定；重命名按旧路径删除与新路径新增处理。范围判定失败时不允许静默跳过检查。

工作流本身始终启动；不使用工作流级路径过滤，以免必需检查一直等待。`Protect main` 规则要求以下四个精确名称：

- `Frontend build`
- `Backend tests (core)`
- `Backend tests (channels)`
- `Backend tests (drift)`

后端矩阵只用取消状态控制是否启动，不能在 job 级按改动范围跳过：GitHub 会在矩阵展开前计算 job 的条件，跳过后只生成带原始表达式的任务名称，三个必需检查将永远缺失。无后端改动时，每个分片仅执行轻量范围判断，明确完成检查，不检出源码、不安装依赖、不编译或执行后端测试。有后端改动时，各分片照常执行。前端使用固定任务名，跳过时仍能满足对应检查。

只在范围判定成功且对应输出明确为 `false` 时跳过耗时验证；判定失败、缺失或异常输出保守执行完整检查。失败、取消、超时不改写成成功，也不使用 `continue-on-error` 掩盖结果。

以上行为来自 [GitHub 工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idif) 和 [必需状态检查排障说明](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)。

## 回归检查与合并前核对

CI 自身的变更会保守执行全量验证，并在 core 分片运行独立回归检查；它们不加载应用或真实账号。本地可用：

```sh
python -m pip install -r .github/requirements-ci.txt
python -m unittest discover -s .github/tests -v
```

回归检查验证稳定门禁名称、矩阵展开、无关步骤跳过、范围错误回退和真实 Bash 判断，并回放原先“工作流成功但缺三项检查”的 GitHub 数据。

不能只看整个 Actions 运行成功或 `gh pr checks --required` 的已报告项目；缺失的必需检查可能不会被列出。使用当前分支保护规则与 PR 最新提交的检查逐项核对：

```sh
python .github/scripts/required_checks.py --pr 212
```

该工具只读 GitHub 规则和检查，退出码 0 表示全部必需检查满足；1 表示缺失、等待或失败；2 表示读取失败或保护规则异常。它不绕过分支保护，也不替代审批、讨论解决或分支同步等其他合并条件。

CI 修复合入 `main` 后，已有 PR 需同步最新 `main` 并推送，重新触发 `pull_request` 检查。对旧运行点击重跑会继续使用旧工作流，不能补齐从未生成的矩阵名称。不要临时删除后端门禁，也不要手工伪造检查成功。

## 后端验证与本地复现

CI 安装 `requirements.txt`，按 core、channels、drift 三个独立分片运行完整后端测试集，各分片内串行且不重试失败。core 执行顶层用例并排除单独的 Drift 素材 API 文件；channels 执行 `tests/python/channels`；drift 单独执行 `test_drift_meme_api.py`。本次只修复必需检查的稳定报告，不扩大测试进程内并行度。

本地复现时，先创建并激活独立虚拟环境，再安装下面的依赖。不要只通过 `--system-site-packages` 借用系统依赖：应用重建测试会隔离子进程，并且只显式加载当前虚拟环境的依赖目录；借用的系统包在子进程中不可用。

```sh
python -m pip install -r requirements.txt
python -m pytest -o faulthandler_timeout=90 --durations=15
```

`--durations=15` 在 CI 日志中列出慢测试，后续优化以真实耗时为依据。

Windows 下应使用较短的工作区路径。若默认用户临时目录过长，可在测试命令后添加 `--basetemp=.pytest_cache/w`，将 pytest 临时文件放到仓库内的专用短目录，避免任务日志的嵌套路径超出 Windows 路径长度限制。该目录仅供 pytest 管理临时数据。
