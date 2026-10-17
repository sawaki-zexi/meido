# Drift 表情维护本地实证（#191）

状态：实现、完整 copy 重建闭环与最终 50 项 core 回归已本地实证；完整两轴审查和总 PR 集成待完成。不作为已集成或 CI 通过声明。固定提交后的完整 Git checkout 字节证明随外部最终交接包交付。最后核验：2026-10-17。

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

LF 与取消后无额外请求断言修正后的最终 core 为 `drift191-cycle31-fresh-checkout-green-final-core-96998ea947`：50 pass、0 skip、0 warning，pytest 日志 171.21 秒（JUnit suite 170.921 秒）；615 个当前输入前后 SHA 全相同，50 份原应用 owner 关闭证明确认，vault 237 次空 get/0 set，无新增线程或非 localhost 连接。原 exec 29525 自然终态 exit0，守卫在原 owner 退出后恢复。此前 `drift191-final-core-scope-v1-916995253c` 的 49 项通过仅证明修正前源，不替代这次最终 50 项。

`drift191-cross-scope-v1-0fa2574083` 的 522 项实际为 516 pass、5 fail、1 skip，耗时 920.26 秒；615 个输入前后 SHA 不变、49 份原 App closer 证明均确认，vault 2109 次空 get/0 set，无新增线程或非 localhost 连接。5 项都在旧 `test_builtin_skills.directory_link` 启动 `powershell.exe` 时 WinError2，尚未进入产品断言。新 v4 runner 只把实际系统 `WindowsPowerShell/v1.0` 加到清理后的固定 PATH；30 轮原 builtin 13 项全部通过，包含这 5 个真实 junction 场景。未把原失败整组改称全绿。

最终 UI 在本地完整整合树重新运行：`drift191-ui-combined-full-v1-b4e341845531` 259 项/45 文件通过；`drift191-ui-combined-build-v1-50613567e37d` TS 与 Vite 均 exit0、109 模块。132 个前端输入前后不变；不是只沿用子组件 33 项。

准确 f4ac 完整 backend tar 归档、同一 18 个既有文件加 9 个新增文件、相同 flags 的 mypy 2.1.0 检查，LF 修复后 `drift191-static-types-v2-dd0d7ce9e6/result.json` SHA `dce72ef33f0fcbbff4d929342e042bd20274a00a7f252af2fb35118bd3d243c5`：58→58，按准确未改源行/列/完整消息及消息内引用行映射，0 新诊断；483 个 backend/test 源通过 `compile()`。两侧 mypy 原退出码仍为 1，不是全仓类型绿色。最初类型 58→100 原件保留，新增 JSON/fixture 注解及缺失 Path 导入已修，不忽略新诊断或改旧基线。mypy API 捕获文本的 LF digest 与 Windows 导出文本文件的 CRLF 原字节 SHA 分开记录，不宣称二者相等。

发布字节检查发现仅加 `eol=lf` 尚不足：原工作区 SKILL 的 CRLF 字节匹配旧常量，但真实 Git checkout 转为 LF 后公开 catalog 拒绝该 builtin。31 轮 `drift191-cycle31-fresh-checkout-red-v2-6289ad710c` 在实际 private-index checkout、`core.autocrlf=true` 后复现 `invalid`；最初找不到 Git 的装配失败另留原件，不算产品 RED。修正 SKILL 物理 LF 与宿主 hash 为 `972dc06af0cd269d6d9b2900448c2bd900be6d75b1c0f30c181e1c8bb35976c9`，profile JSON 仍为 `62143d5248aa431f78b28a6489ef409e8ca40667eab557a866b8e69ddd614c1a`；普通正文与版本不变。最终 50 项中的新测试通过公开 catalog 验证真正安装字节，未用 archive 代替 checkout。该 RED/GREEN 原件另经 root 独立核实，proof `drift191-cycle31-root-verification-v1.json` SHA `df5f65af8fd6e3f2e98aca6cdd919ab872c1a6ceea9a41a478f6bc98c7253bcf`；proof 中原 handle 当时待确认的历史措辞由随后作者收取的权威 exit0 补齐，不回写原件。

## 失败原件与界限

19 轮第一次 `drift191-cycle19-prepublication-red-a11b1a8d7e` 是人为中断，原 closers 未证明；不纳入成功清理或产品 RED。后续 v2 有真实 staging hash 持发布门导致停用超时的反例，green 将长准备移至锁外；watchdog 仅释放 fixture Event，未代替原 owner。

21 初次及 v2 是夹具断言/Windows 过长临时路径装配失败，原件保留；v3 使用短隔离子目录完成真实两进程验收。28 两个早期 decode 尝试分别由 fixture 重入 getpixel、PNG 初始 load 返回 None 导致，均不算产品 RED；resize 已通过，最终 decode `drift191-cycle28-image-decode-qualification-v3-a630a6d75a` 1 pass。原 29 轮 S06 回归准确发现 off 用途原因遗漏普通主人聊天说明，补正确原因后原 S06 通过，旧测试未放宽。30 初次 phase1 copy 后沿用旧 manifest 观察被正确 CAS 拒绝，fixture 未核类别便 finish，phase2 原图片/类别断言因此失败；v2 实际重新读 manifest 再写，并核验两次真正类别，未放松 CAS 或添加业务自动重试。

02/05/09/16 中目录虽带 green、实际 exit1 的中间尝试也保留：分别是 fixture Path 使用错误、409 原因误映射为 422、配额原因未正确公开、authoring off 仍 pending。只有其后实际通过原件列入相应闭环。旧 07 尝试的 guard-restored 字段未证，不补写成已确认。

存储按实际文件与完整 UTF-8 持久字段计量，SQL 的 256 字节/行是原政策费用，256 项另含一个共享 journal 容器责任；不宣称共享 SQLite 物理文件被硬限至 64MiB。关闭后的旧有限预留保留自主/global 责任，新普通 owner 编辑退出新增 quota。原业务无 FS/SQLite 原子事务，最终目录可能暴露完整但 partial 的新图；外部同账号 OS 程序不受协作锁约束，ABA 仅在可观察检查处拒绝或事后 conflict。

真实视觉理解、供应商输入 token/费用、真实账号与投递均未用本地 fixture 代替。通用任意重新编码/变换内容的识别不在已知图像回显投影的实测范围。本页不以旧 aggregate 的绿色 CI 声称本地 #191 实现已获 CI 或完整两轴通过。
