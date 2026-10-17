# 验收 codex-delegate #175

当前总集成补充：固定源`0e2da975`已完成完整两轴及修正复核，无遗留。多仓库诊断与Linux真实文件夹具两P2保留原RED，规范独立Windows14/WSL13、规格独立Windows14/WSL12项通过；实际start/admit未放宽。源经`a7176ce`、`3eac7d5`从f7be合入总分支，私有Coding/Skill/spawn模块与源完全一致；main同时保留主动stop/主人优先、NovelAI事务、CU/所有owner关闭，并在任何await前seal delegation。固定`43fc03a`总组合610 passed/1 Windows符号链接权限skip，原七项Herdr原生夹具实际运行通过；前端202项/构建及集成两轴通过。准确13c两套完整CI已通过：各后端2235 passed/35 skipped/2 warnings、前端202 passed/35文件及TS/Vite，详见[总状态](../reference/shiori-tool-parity-status.md)；原a67的98项不倒写为新准确集成全量。

状态：本地实现与无费用合成验收；独立两轴及总集成 CI 以最终交付记录为准。真实 Codex 认证、模型、首次/resume 任务和实际原生权限未验收。最后核验：2026-10-16。事实来源：[公开 HTTP](../../tests/python/test_codex_spawn_api.py)、[实际 exec 协议夹具](../../tests/python/test_codex_executor.py)、[严格 JSONL](../../tests/python/test_codex_protocol.py)、[恢复](../../tests/python/test_codex_recovery.py)、[off/on 并发](../../tests/python/test_codex_revoke_api.py)、[原渠道](../../tests/python/test_coding_channel.py)、[React](../../web/src/features/coding/CodingSettings.test.tsx)。配置与接口见[reference](../reference/owned-coding-tasks.md)。

本票从已验证总集成 `fff08b186809726a219f280dc8352134e62523c0` 建独立分支，按主人已批准的例外统一进入 PR162；没有创建独立堆叠 PR。#174 的固定原生 bundle、补丁、ConPTY、许可证和全局资源 collector 不变；#175 交付时 #176 commit/publish 未开放，后续实现及无费用证据见[#176 手册](coding-publication-acceptance.md)。

## 设置与用户闭环

1. 在设置→编程委托登记真实 Git top-level、固定验证 argv/cwd 和允许角色。登记专用 CLI，委托方式选 Codex exec，固定 Codex 0.159.3 的程序/哈希、模型与显式专用认证目录；不从桌面登录自动导入。Codex exec 不需要配置 Herdr。
2. 保存只登记配置。未经匹配 profile 的两回合原生核验，状态仍 unverified、Skill 依赖不可用。真实核验会调用账号模型，只有另有明确费用/账号许可才点击同意按钮；本票只使用测试创建的假认证和本地合成 CLI。
3. 角色资料选择 repo/profile/default Codex exec，开启 Coding 与独立子任务。保持其他 Shell/CU/NovelAI 配置。显式 `$codex-delegate` 或 skillIds 激活后，传完整需求与获准 repository 给 spawn coding，明确后台 true、retry_count=0。
4. 父回合完成后仍可 get/list，完成只回原会话；追加要求通过 spawn_manage continue，同 job/new attempt/exact UUID；cancel 等待本地收尾。关闭再开启 spawn 不复活旧回合或重发未知 prompt。
5. 查看实际 validated_diff、固定验证、cleanup 和 ACK 事实。取消不回滚编辑/已用模型；不能把合成协议成功、exit0 或模型摘要当作真实权限、commit 或发布证明。

CLI 与获准仓库程序使用同一 Windows 账号的能力。这是可信 repo/CLI 模式，受管 Job、最小环境和 canary 不构成恶意同账号代码的 OS 沙箱。

## 隔离运行

pytest 收集与 import main 前设三个不同临时根：MEIDO_DATA_DIR、MEIDO_ROLES_DIR、MEIDO_OWNER_KNOWLEDGE_DATA_DIR。整个命令使用 local-only socket/DNS/asyncio guard，持续到 Runtime、Job、writer/readers、provider HTTP、OneBot WebSocket 与 avatar worker 全部 join；不能仅在单个测试函数内安装 guard。新 HTTP 夹具关闭与其目标无关的自动记忆，内存功能断言不变。

专用 `exec_cli.py` 仅通过受信测试 process_factory 启动本机 Python PE，接收真实 pipe UTF-8 stdin、固定 argv/cwd/env，产生分块 JSONL、新结果和实际工作树修改；没有生产 test bypass、真实模型或认证。Codex 模块测试不需要下载 CLI。兼容 #174 的七个原生测试仍需其明确配置的固定 Herdr bundle 与 Rust 合成 launcher；准备方法见[原生手册](coding-native-build.md)。

```powershell
# 三项 MEIDO 根与 whole-command guard 必须先由测试启动器建立。
$files = rg --files tests/python | Where-Object { $_ -match 'test_(coding|codex).*\.py$' }
python -X utf8 -m pytest @files -q --basetemp=D:/fresh-owned-test-root
python -m mypy --follow-imports=silent --ignore-missing-imports --no-incremental backend/app/coding_tasks backend/app/agent_runtime/spawn_tool.py backend/app/agent_runtime/skills.py backend/app/agent_runtime/capabilities.py
python -X utf8 -m compileall -q backend tests
npm run test:web
npm run build
git diff --check
```

使用全新属于该轮测试的 basetemp，确认路径后再运行，pytest 会管理它。较短基根用于兼容原 Herdr IPC；Codex 公开 HTTP 用例额外增加目录层级，实际 owned 根约80字符，至少覆盖普通 Windows AppData/Meido/data/coding 深度。更深的早期 pytest 根曾使合成 transcript 创建失败，原始失败未隐去；已压缩 Codex 资格探针中重复的 task 目录，并成功重跑正常深度。没有修改系统长路径策略或真实数据。不能把正常深度合成成功推广为任意深度或真实 CLI 长路径兼容。

## 本地证据

作者外部证据位于 D:/meido-artifacts，以下各轮不能相加或混称准确最终 HEAD：

- spawn 构造 seam、JSONL 模块、unread-stdin/流式 stdout 先保留 RED，再完成 GREEN；首个原生 exec/resume 整组17项绿，后续相关路由/配置/普通 spawn 30项绿。
- 公开 localhost OpenAI SSE provider→显式 Skill→主人设置/两回合合成 probe→Codex exec→SQLite/Runtime→原会话：正常深度1项通过（30.51秒），六个主人回合恰好12个 provider HTTP 请求，无子代理或摘要模型。首轮深路径失败、后续 list wire 断言修正均保留。
- 完整 Coding 组95项通过（234.98秒），含 #174 七项原生合成、Codex stdin/JSONL/实际 Git 与验证、11类坏协议/旧结果、copy/ACK故障和两种 executor 的真实 OneBot 来源。该轮之后又收紧 off/on 封门顺序，因此不是最终 HEAD 全量。
- 实际 Runtime 接受返回丢失、真实 SQLite taskId 写入失败、ACK后dirty清理失败且结果prune，均保持原 attempt/key，业务 executor 不重跑；原来源失效时仍 copy/ACK，交付抑制不转桌面。
- 真实公开 role off/on 请求被旧普通 spawn cancel 阻塞时，旧冻结 Coding 权限曾 RED；封门移到任何 await 前后，专项10项 GREEN。原始 red/green XML 为 coding175-revoke-http-red.xml / coding175-revoke-http-green.xml。
- 前端全32文件183项通过（30.08秒），之后默认 Codex profile 保存/重开断言专项3项通过；TS/Vite和19源默认 mypy通过。默认 mypy不检查全部未注解函数体，不写作严格类型覆盖。
- 共享107项 pytest 虽通过，但 whole-command guard挡住并记录11次 dashscope 连接尝试，该命令为失败；Skill HTTP单独定位到默认记忆配置，随后在非记忆夹具中显式禁用。未发生真实外网、账号或模型费用，未将该轮称作完整隔离通过。最终修正后的准确检查另记交付。

实际 CLI 仅运行过私有空 HOME/CODEX_HOME 下的 --version、exec --help、exec resume --help，识别版本0.159.3；未执行真实账号任务。resume help不支持 --sandbox/--cd，当前通过固定config恢复权限，仍必须在获准真实两回合探针中证实。合成 native refusal JSONL 明确是测试产物，不能用于宣称真实 Codex sandbox 或认证隔离已验收。

后续专项记录：Skill夹具隔离后32项通过（9.09秒），剔除由#172协调修正的直接Shell夹具后，共享spawn/Skill/能力/角色组88项通过（44.58秒），两轮whole guard均为0。直接Shell独立19项pytest虽通过，7次dashscope尝试被挡，仍为失败命令；该夹具未在175重复编辑。没有为绕过该问题关闭生产记忆或削弱记忆断言。

之后完整Coding组96项通过（271.08秒）；再补有界正常中断请求和可行动的协议错误码，相关29项通过（46.28秒），公开HTTP/恢复/配置18项通过（34.64秒）。CLI已实际完成但业务needs_input的两状态区分曾RED，修正后协议/实际exec专项24项通过（35.51秒）。Windows无console子进程可能拒绝CTRL_BREAK请求，宿主直接升级Job，并以真实进程及writer/readers退出作为收尾证据；不伪称优雅停止。19源默认mypy、compileall及diff检查通过，完整前端最终32文件183项通过（32.34秒），TS/Vite通过。最终冻结后的完整Coding结果以交付记录为准，不把前述较早96项写成涵盖后补分支。

原固定提交 `a67a07018bdcb2fcab0627242102fd9fb982bbd6` 的完整Coding复跑 **98 passed（248.40秒）**，`coding175-immutable-coding-native.xml`；七项原Herdr原生夹具均实际执行，没有skip，Codex exec公开HTTP、87字符正常深度根的先前独立验收、实际stdin/JSONL/Git、原生终态/needs_input、恢复/ACK、原渠道、off/on、受管中断与收尾均有相应真实本地夹具。whole-command guard零非本地尝试，三独立MEIDO根在收集前建立并保留至全部owned退出。未复跑完整后端：其余旧HTTP记忆夹具由#172和协调者独立收紧，不能把本票98项或88项共享回归写成全后端通过，也不能将该98项当成后续审查修正提交的准确HEAD验收。

## 两轴审查修正的定向证据

独立Standards与Spec交叉确认：角色保存两份仓库后撤销首份，旧availability只核首ID，导致第二份仍可用时误隐藏Skill。另有Linux venv新配置夹具将symlink入口当作固定程序，生产正确拒绝。修正仅令availability扫描当前许可候选、每项仍调用同executor的完整resolve，并在Linux夹具使用`resolve(strict=True)`；没有改实际start、原生路径规则或跨executor许可。

- [候选回归](../../tests/python/test_coding_availability.py) 先 **6 failed /2 passed（15.03秒）**，覆盖首项撤权、旧ID、路径失效和被替换；修正后Windows配置/候选 **12 passed（19.55秒）**。全部不可用仍拒绝，错误default/未核验profile不借另一executor通过。
- WSL Python3.12配置原 **1 failed /3 passed（2.37秒）**；仅修真实可执行路径后，配置/候选 **12 passed（7.49秒）**，保留原非Windows native准入与资格探针拒绝断言。
- 扩展公开HTTP与两份原外部复现 **3 passed（43.78秒）**。完整用例先完成两回合本地合成资格；公开API撤销首仓库后，Skill正文进入下一真实localhost SSE请求。指定已撤仓库的spawn拒绝且不创建任务，再向第二仓库完成真实Python CLI/Git/验证、精确继续、取消、copy/ACK。现在共七个主人回合、14个provider请求，无聊天child或完成摘要模型。
- Windows Skill/普通spawn/Coding路由及角色/工具组 **60 passed（16.65秒）**；WSL公开HTTP及Skill组 **33 passed（27.05秒）**，后者只有现有FastAPI/httpx测试客户端弃用警告。各组没有skip或非本地阻断；重叠用例不相加为独立总量。
- 本轮19源默认mypy与compileall通过；默认mypy仍不检查全部未注解函数体。没有因这两项窄修正重跑旧98项、完整后端或完整前端测试；真实Codex账号、首次/resume模型及原生权限资格仍未验收。修正固定头另交独立两轴增量复核与总集成CI。

以上均通过收集前的三独立MEIDO临时根与whole-group/outer local guard运行，持续到owned Runtime、进程/Job、管道、HTTP及worker收尾。外部JUnit统一为`D:/meido-artifacts/coding175-fix-*.xml`，原RED保留；此处不将作者定向GREEN写成独立复核完成。
