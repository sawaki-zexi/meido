# Drift 表情维护本地实证（#191）

状态：完整两轴发现的问题已修正，本地新组合 151 项通过；固定 c38 的原 50 项及 checkout 保留为历史证据。新固定头增量两轴和总 PR 集成待完成，不作为已集成或 CI 通过声明。固定提交后的完整 Git checkout 字节证明随外部最终交接包交付。最后核验：2026-10-17。

事实来源：[批准规格](../specs/drift-meme-maintenance-spec.md)、[HTTP 与真实 tick 测试](../../tests/python/test_drift_meme_api.py)、[完整应用子进程](../../tests/fixtures/drift_meme_rebuild.py)、[逐协议实际发送](../../tests/python/test_model_image_request_budget.py)、[稳定契约](../reference/drift-meme-maintenance.md)、[验收手册](../handbook/drift-meme-maintenance-acceptance.md)。实现基点为双 CI 验证的 `f4ac973a04cdcd8327f80f20bd58d5088fdf8b77`。唯一总 PR 为 [#162](https://github.com/sawaki-zexi/meido/pull/162)。

## 实测范围

所有实际模型通信均为 localhost fixture；素材由 Pillow 自制，不访问真实凭据、模型、账号、平台或费用。四个新绝对 MEIDO 根与新 HOME/TMP 在任何 app import/collection 前设置；清理继承配置，空 vault get/拒绝 set，内外 localhost 守卫保持到原 owner 自然关闭。原 TestClient portal、TaskRuntime、应用 memory/channel closers、线程与子进程 Job 沿原拥有关系结束。

已提取的组件独立交付为 UI `d09b8d55bb49677ae47d14a8fa025754302c704c` 与 adapter `91a70603ff77496471c891185fe59b6b0fcf22a3`，顺序整合成本地 `52c33e635390376af4971a1bbffeab16d194eafb`。UI 相关 33 项、TS/Vite 通过；adapter 新增 81 项、原协议 105 项、最终受影响 29 项分别通过。准确 `f4ac→52c33e6` 的组件 Standards/Spec 两轴均 0 findings，范围仅 UI+adapter，不代替 core 接线、组合验收及最终完整复核。

## 规格逐项证据

下列原件均位于外部 `D:/meido-artifacts/`。每个 pytest 目录保留原日志、JUnit XML、执行前后输入 SHA、环境名字、vault/网络与原 owner 清理证明。目录后缀区别每次不可覆盖的尝试；数字是实际测试结果，不能将资格检查中的已有通过伪称新 RED→GREEN。

| 规格 | 实际观察与代表原件 |
| --- | --- |
| §4.1 默认 off、Shell-off、旧库真实视觉/分类、独立 authoring | 1/2/16 轮；`drift191-cycle02-green-v2-b170458603` 与 `drift191-cycle16-green-v2-77d6c5727c`（2 项）。640×480 旧图实际进入下一请求，类别/别名改变、其他类别保留，silent/nextAction 持久；authoring off 同样完成 |
| §4.2 上传、窄复制与完整后续采用 | 4/13/30 轮。13 轮明确 copy 与分类 edit 分开。`drift191-cycle30-copy-rebuild-qualification-v2-043d933553`（1 项）通过原时钟真实上传素材 copy；模型从真实宿主消息取 ID，原字节/metadata/分类核验后关闭整应用。进程 18392→90024，第二进程下次 tick 实际读同一新 copy 并维护类别，随后普通主人实际读同图；Drift 各图片请求 1、主人图片请求 1，progress 1→2，同副本 SHA `e2f812…` 不变 |
| §4.3 CAS 与物理阶段 | 3/6/7/11/12 轮；`drift191-cycle11-recovery-boundaries-679170a0ce`（4 项）。实际 image 前、metadata 后、manifest 后与 SQL 切点分别记录阶段；恢复不重发模型/copy，未知保留，后续主人类别不回滚 |
| §4.4 实际逐 send 预留 | 17/25 轮；`drift191-cycle25-wire-multiplicity-qualification-86391ac4d3`（3 项）。原 Routed serializer+真实 localhost，body image 次数分别 `[0,0,1,1,1,1]`、同 payload 双图 `[0,0,2,2]`、历史重带 `[0,0,1,2]`；服务器到达前已见每条原 run 持久账本。超 4 曝光在 socket 前拒绝，累计估算按实际次数×8192 |
| §3.6 正常模型回显不持久化 | 14/15/22 轮；`drift191-cycle22-structured-echo-red-c9286cb789` 真 2 fail/2 pass → `drift191-cycle22-structured-echo-green-a3208c513b` 4 pass。覆盖两段/32 字符文本、整图字典键与参数字符串数组；实际下一请求及 SQLite 不留已知图片片段 |
| §4.5 完整存储峰值 | 8/9/10/20 轮。真实 64MiB、256 项、其他角色 16×64MiB=1GiB 文件反例，保留未知文件；`drift191-cycle20-journal-peak-green-fc9729d02a` 将实际完整转义 JSON、expected 与原/新副本加入 staging 前预留 |
| §4.5 固定身份与原许可 | 18/26/27 轮。`drift191-cycle26-identity-qualification-1047cb4457` 7 pass：未激活、S06、经公开 preview/adopt 的同名 role 技能及伪 builtin 正文 hash；`drift191-cycle27-original-scope-qualification-5b56fbbb11` 6 pass：错误/另角色源、类别、源 bytes、模型换代、真实 `os.link`/nlink2。root ABA 与支持文件改变在原 18 轮拒绝 |
| §4.5–6 原资源自然收尾 | 5/19/24/28 轮。`drift191-cycle24-provider-finally-qualification-d2628fb128` 2 pass：真实图片进入原 provider 后持有 finally，新主人输入/disable/stop×3 不二次取消，主人真实写继续，旧 ACK/重新准入等待 join。28 轮实际 Image.load PixelAccess/thumbnail resize 与 fsync/hash owned 线程同样验收 |
| §4.6 禁用后原路径次序 | 23 轮：`drift191-cycle23-disabled-admission-red-7476bc818c` 真 1 fail → `drift191-cycle23-disabled-admission-green-81d5c93472` 1 pass。旧已准入 owner 被 seal 后不降格无锁 replace；新 disabled owner 按原路径锁等待后普通写入 |
| §4.6 未决 journal 与全应用重建 | `drift191-cycle21-rebuild-v3-0ce8db6844/isolated/r21/phase-{1,2}-result.json`。真实进程 59288→90592，copy partial/files_published→disabled→完整关闭重建→owner 直接改 manifest 并实际读同新图→原 intent conflict；同副本 SHA `e2f812016044ba0216128c06ef2ec2e3e0d4263fe3b48a2ee6a7905d0d12eb23` 保留，旧 copy/模型未重放 |

21–23、24、25–26 与 27 原件另经 parent 独立读取源/XML/manifest/实际 phase 复核。对应 root proof 的 SHA 为 `9fc3744216581d8c8f4f2fd469a4482eaccd6215b56fa9c45c5caf2c3f901ccf`、`08a34b7d298e4afb0429cee9205554d5a9976be1376d1b933c8742bfd8659963`、`a93726d4f0551db77fb087ec2fc8a3aec384a23c52a4284860d6a65a5373b3a4`、`3baa49f5cea5473c1fa363d6a4404c800d3fd2f1f216116cc8bd8ede7c706c46`。1–29 的 62 次原件优先索引为 `drift191-cycles-1-29-evidence-root-v2.json`，SHA `ad255bf36eb7213e5fb9e936fa207838d8787b06f2865b21f8a9da3a2466ff95`；其中 188 份原文件已经 root 重算，不按目录名认定绿色。

最终衍生索引 `drift191-final-evidence-index-v3.json` SHA `3d24a7eb115bc26c56e7a0b15ae9387c65e90ad42189093527628a71eb49c18b` 原样保留上述 62 条，另收录 30–31 轮的 5 次原尝试、组合与最终静态检查；不跨重复测试累计通过数，不覆盖旧索引或原日志。

## 组合与静态检查

固定 c38 时 LF 与取消后无额外请求断言修正的 core 为 `drift191-cycle31-fresh-checkout-green-final-core-96998ea947`：50 pass、0 skip、0 warning，pytest 日志 171.21 秒（JUnit suite 170.921 秒）；615 个当时输入前后 SHA 全相同，50 份原应用 owner 关闭证明确认，vault 237 次空 get/0 set，无新增线程或非 localhost 连接。原 exec 29525 自然终态 exit0，守卫在原 owner 退出后恢复。此前 `drift191-final-core-scope-v1-916995253c` 的 49 项通过仅证明更早源，不替代这次 50 项或后述新组合。

`drift191-cross-scope-v1-0fa2574083` 的 522 项实际为 516 pass、5 fail、1 skip，耗时 920.26 秒；615 个输入前后 SHA 不变、49 份原 App closer 证明均确认，vault 2109 次空 get/0 set，无新增线程或非 localhost 连接。5 项都在旧 `test_builtin_skills.directory_link` 启动 `powershell.exe` 时 WinError2，尚未进入产品断言。新 v4 runner 只把实际系统 `WindowsPowerShell/v1.0` 加到清理后的固定 PATH；30 轮原 builtin 13 项全部通过，包含这 5 个真实 junction 场景。未把原失败整组改称全绿。

固定 c38 的 UI 在本地完整整合树重新运行：`drift191-ui-combined-full-v1-b4e341845531` 259 项/45 文件通过；`drift191-ui-combined-build-v1-50613567e37d` TS 与 Vite 均 exit0、109 模块。132 个前端输入前后不变；不是只沿用子组件 33 项。

准确 f4ac 完整 backend tar 归档、同一 18 个既有文件加 9 个新增文件、相同 flags 的 mypy 2.1.0 检查，LF 修复后 `drift191-static-types-v2-dd0d7ce9e6/result.json` SHA `dce72ef33f0fcbbff4d929342e042bd20274a00a7f252af2fb35118bd3d243c5`：58→58，按准确未改源行/列/完整消息及消息内引用行映射，0 新诊断；483 个 backend/test 源通过 `compile()`。两侧 mypy 原退出码仍为 1，不是全仓类型绿色。最初类型 58→100 原件保留，新增 JSON/fixture 注解及缺失 Path 导入已修，不忽略新诊断或改旧基线。mypy API 捕获文本的 LF digest 与 Windows 导出文本文件的 CRLF 原字节 SHA 分开记录，不宣称二者相等。

发布字节检查发现仅加 `eol=lf` 尚不足：原工作区 SKILL 的 CRLF 字节匹配旧常量，但真实 Git checkout 转为 LF 后公开 catalog 拒绝该 builtin。31 轮 `drift191-cycle31-fresh-checkout-red-v2-6289ad710c` 在实际 private-index checkout、`core.autocrlf=true` 后复现 `invalid`；最初找不到 Git 的装配失败另留原件，不算产品 RED。修正 SKILL 物理 LF 与宿主 hash 为 `972dc06af0cd269d6d9b2900448c2bd900be6d75b1c0f30c181e1c8bb35976c9`，profile JSON 仍为 `62143d5248aa431f78b28a6489ef409e8ca40667eab557a866b8e69ddd614c1a`；普通正文与版本不变。最终 50 项中的新测试通过公开 catalog 验证真正安装字节，未用 archive 代替 checkout。该 RED/GREEN 原件另经 root 独立核实，proof `drift191-cycle31-root-verification-v1.json` SHA `df5f65af8fd6e3f2e98aca6cdd919ab872c1a6ceea9a41a478f6bc98c7253bcf`；proof 中原 handle 当时待确认的历史措辞由随后作者收取的权威 exit0 补齐，不回写原件。

## 完整两轴复核后的修正

准确 `f4ac→c38ab118193b46c790b8dce75788024ef6961018` 完整 37 文件复核：Spec 初报告 2 项，另类别补充后合计 P2 跨响应原图直拆、P2 类别许可、P3 保存时机提示；Standards hard 0、possible P3 大位置参数 1。原报告与 c38 冻结保持不变，不能沿用组件 0 findings 宣称整项已通过。修正计划公开于 [#191 comment6227203870](https://github.com/sawaki-zexi/meido/issues/191#issuecomment-6227203870)。

32 轮先用真实 read_file→下一 provider 自制 1×1 PNG，再跨独立响应输出 32/32/28 片段，合法 list_dir 或实际 write_file.content 往返，最后仍完成正常分类与 nextAction。`drift191-cycle32-cross-response-echo-red-d937809955` 4 fail → `drift191-cycle32-cross-response-echo-green-a69d212c9b` 10 pass（含旧 4/正常 2）。单一 offset 首版仍会被正常短值毒化，且未处理 thinking/签名：`drift191-cycle32-cross-response-variants-red-f2fc4de38e` 准确 9 fail/5 pass → `drift191-cycle32-cross-response-variants-green-a52296e06d` 20 pass。新增 14 项覆盖两种分片边界×text、参数 content、首字符噪声、thinking、thinking signature、text signature、tool thought signature；检查全部持久历史版本而非只取最后一行，原工具实际文件也不能重组整图。原 owner 均自然结束；root 独立核验 proof SHA `7519bc51a03d713dfda7e9cf20e87afe57b5e4302aac957b190f6fe34f075ca4`。

有限守卫保留每个已知原图的多个可达原串前缀，0 分支持续可用，正常元数据不重置匹配；文本、thinking、签名、参数及组合顺序参与，完成整图的当前字段在 response 持久化/dispatch 前投影。`drift191-echo-capacity-qualification-v2/result.json` SHA `49118061d8d0099668b2cac2e6a9af16faee04cadb470d5c80154be3a37dd660` 以真实自制 PNG、实际 model_types、准确 AST 验证：2048 状态处正常业务仍保留，新增状态固定失败；131072 工作界前正常多 block 保留，超界固定失败。该纯资格的 sink 是返回后模拟回调，不是 HTTP/owner 证明；root 另核 proof SHA `6de75d2bc21a5eca294ce2eb7782a5877202bf0fbeb2b64aa4f4fe4ffd1a9664`。首容量 v1 仅因 HOME/USERPROFILE 同新路径重复 mkdir 在 app/模型 import 前装配失败，不算产品 RED。

33 轮 `drift191-cycle33-category-grant-red-552490478e` 准确 3 fail/1 pass：许可 happy 时，旧 manifest 的 happy/../secret 与 happy/nested 实际写出新图；manifest.json 样式导致异常，secret 原本正确拒绝。root 在固定 c38 独立真实 HTTP 也证实图被写到 secret，纵使 tool 文本 generic rejected；proof SHA `fe32a3629ee914a07113a64e85158312568cc1d7e2c98a0ae9d0a47ab429d61f`。修复在开始、staging 与最终发布均按 copy 的精确冻结类别资格，不借 read_manifest 首段例外。UI 仅将归属提示改为运行前核验，不新增保存行为。

复制发布边界由 23 个位置参数收拢为有限 frozen CopyPlan 与三个 immutable 文件计划，完整 journal JSON、原源身份与字节/hash 仍固定。34 轮 `drift191-cycle34-copy-rename-fact-red-9d7b6cbf97` 真 1 fail：OS rename 返回且新图实际存在，但其后身份与恢复均无法确认时仍宣称 files_ready。修复先保存 rename 返回事实与物理阶段，再核身份，未知仍为 conflict。首组合 `drift191-cycle33-34-copy-plan-green-62caa0b866` 实际 10 pass/1 fail，原因是 fixture 用 PermissionError 按原合同继续下一模型却仍断言 3 次；改 OS 身份故障 OSError 后保持原 abort 合同，未改变生产业务重试。

第二组合 `drift191-cycle33-34-copy-plan-green-v2-029736d2e4` 仍是 10 pass/1 fail，真实并发恢复先读同一 revision，后来的事实投影把另一原恢复已 recorded 临时降为 conflict；不能当夹具失败。35 轮 `drift191-cycle35-concurrent-recovery-red-f911de5aac` 以两个原 owned 恢复在实际 manifest 事实处受控交错，真 1 fail，JUnit 记录第二恢复在第一 held 时已返回；无 sleep，watchdog 只释放 fixture Event，原两个 owner 仍 join。同角色独立恢复门串行处理该原事实，不占主人 path/app gate，不放宽 owner 版本检查。`drift191-cycle33-35-copy-recovery-green-884b5d7321` 13 pass 覆盖类别 4、阶段 6、正常 copy 2 与 disabled 整应用重建 1。

修正后 UI 相关 `drift191-ui-review-correction-related-9efabbc15c52` 33 pass/6 文件，`drift191-ui-review-correction-build-0cc306d832a0` TS/Vite exit0、109 模块。未因提示文字重复全前端 259。新类型 `drift191-static-types-v2-03a5ffb9e9/result.json` SHA `b9daa7904c6cf07282d667eb6b5141fad49ed1a5e60576d804a12a2708efba94` 仍准确 f4ac 完整归档、18 既有+9 新增、58→58/0 新、483 compile；两侧 mypy exit1。中间 58→60 仅两处新测试 tuple/None 注解错误，原件保留并已修，不忽略诊断。

新完整 `drift191-review-corrections-final-core-adapter-02386a3fab` 为 151 pass（70 core+81 adapter）、0 fail/error/skip/warning，日志 300.76 秒/XML 300.497 秒。615 个执行输入前后与当前源匹配，70 份原 App 关闭证明（不是 151 份 App）、vault 254 次空 get/0 set、无新增线程或非 localhost 错误；原 exec30563 自然 terminal exit0，守卫在原 owner 退出后恢复。新的不可覆盖 `drift191-review-corrections-evidence-index-v4.json` SHA `0d62cc159fe9aab9ecc945badc3e713d2779c6eb405513b2868006054d169147` 完整原样包含 v3，再追加 32–35 的 10 次原尝试与新组合、容量/类型/React/原报告。root 独立 final151 proof SHA `f886bd4fa01a1452614006cfdf93ca64d7f27b5c787299ded9a5348e6f3bcbe4` 保留写时原 handle 待作者确认的历史措辞，随后作者权威 exit0 补齐，不回写原件。旧 50/20/13 不代替该组合，原 c38 报告也不冒新头增量两轴已通过。

并发恢复的参与者资格补充：旧 13 项及 final151 的 `second_recovery_finished_while_first_held=True` 受全局 ordinal 截断影响，后台恢复可能消耗名额，不能据此确认两个指定 actor 都进入原 reconcile；最终 recorded 的行为证据仍有效，旧原件保持不变。新同一封存外部 fixture SHA `89e8ea6735c4940b8d227578b65779ce2592ad2b8d99f7976fdad1de97cf823f` 在固定 c38 的 `drift191-specified-recovery-actors-red-eef3b1294e` 真 1 fail（8.33 秒、final conflict），在 0d7 生产源的 `drift191-specified-recovery-actors-green-497710a14e` 真 1 pass（9.51 秒、final recorded）。ContextVar 标记的 A/B 各委派原 reconcile 恰好一次，记录各自不同线程的 entry/return；背景分别暂缓 0/2 次。RED 由 B 原方法返回释放 A，GREEN 由有限 watchdog 释放 fixture Event 后两个原方法返回；不宣称观测了 native gate attempt/acquired。原句柄 7501/54505 均自然终态（exit1/0），原 owner 关闭与 guard 恢复均确认，0 live handle；root 独立核验 `drift191-specified-actors-root-verification-v1.json` SHA `309f7b2fc05a2fe082efc5947ae39cf29c986c4234f56b5e414fcf1006387df0`。本次仅修正测试与本页，未改生产或重跑 151/前端/ASR；随后只补局部显式类型，旧 0d7 的 58→58 类型结果限于当时测试源，窄静态辅助在 mypy 前预检停止，当前测试类型由统一 CI 检查。

## 失败原件与界限

19 轮第一次 `drift191-cycle19-prepublication-red-a11b1a8d7e` 是人为中断，原 closers 未证明；不纳入成功清理或产品 RED。后续 v2 有真实 staging hash 持发布门导致停用超时的反例，green 将长准备移至锁外；watchdog 仅释放 fixture Event，未代替原 owner。

21 初次及 v2 是夹具断言/Windows 过长临时路径装配失败，原件保留；v3 使用短隔离子目录完成真实两进程验收。28 两个早期 decode 尝试分别由 fixture 重入 getpixel、PNG 初始 load 返回 None 导致，均不算产品 RED；resize 已通过，最终 decode `drift191-cycle28-image-decode-qualification-v3-a630a6d75a` 1 pass。原 29 轮 S06 回归准确发现 off 用途原因遗漏普通主人聊天说明，补正确原因后原 S06 通过，旧测试未放宽。30 初次 phase1 copy 后沿用旧 manifest 观察被正确 CAS 拒绝，fixture 未核类别便 finish，phase2 原图片/类别断言因此失败；v2 实际重新读 manifest 再写，并核验两次真正类别，未放松 CAS 或添加业务自动重试。

02/05/09/16 中目录虽带 green、实际 exit1 的中间尝试也保留：分别是 fixture Path 使用错误、409 原因误映射为 422、配额原因未正确公开、authoring off 仍 pending。只有其后实际通过原件列入相应闭环。旧 07 尝试的 guard-restored 字段未证，不补写成已确认。

存储按实际文件与完整 UTF-8 持久字段计量，SQL 的 256 字节/行是原政策费用，256 项另含一个共享 journal 容器责任；不宣称共享 SQLite 物理文件被硬限至 64MiB。关闭后的旧有限预留保留自主/global 责任，新普通 owner 编辑退出新增 quota。原业务无 FS/SQLite 原子事务，最终目录可能暴露完整但 partial 的新图；外部同账号 OS 程序不受协作锁约束，ABA 仅在可观察检查处拒绝或事后 conflict。

真实视觉理解、供应商输入 token/费用、真实账号与投递均未用本地 fixture 代替。通用任意重新编码/变换内容的识别不在已知图像回显投影的实测范围。本页不以旧 aggregate 的绿色 CI 声称本地 #191 实现已获 CI 或完整两轴通过。
