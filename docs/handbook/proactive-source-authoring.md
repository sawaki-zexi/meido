# 创建并激活主动来源

状态：当前 / #178 本地实施，独立复核及总分支验收另行记录。

事实来源：[作者Skill](../../backend/app/builtin_skills/create-proactive-source/SKILL.md)、[预览](../../backend/app/proactive_runtime/source_authoring.py)、[受管验证](../../backend/app/proactive_runtime/source_verification.py)、[HTTP行为测试](../../tests/python/test_proactive_authoring_api.py)。最后核验：2026-10-16。

## 创建文件

在当前角色输入 `$create-proactive-source 创建离线合成来源`。需要角色文件工具；复用服务还需已获准的MCP发现许可。完整可启动模板在Skill正文；支持文件仅校验hash，不宣称普通read_file可以读取分发目录。模板使用Python 3.11+标准库，不安装包、不访问账号、网络或模型，不需要Shell许可。

角色写入 `workspace/mcp/<name>/` 下的source.py、catalog.json、fixture.json、source-draft.json、manifest.json及README。alert/content持续返回未ACK事件，content携带关联正文；context不ACK。缺工具、解释器、MCP或凭据时保留准确草稿和配置路径，不自动授予权限。真实服务接入须具体服务读取/ACK许可，秘密只通过现有凭据设置保存。

## 核对实际hash

在自主设置“来源产物预览”填写相对路径 `mcp/example/manifest.json`，服务ID可先留空。预览只读当前角色已存在文件，不创建目录、执行或注册。越界、链接/hardlink、超量、缺文件和读取中变化均拒绝。

初稿PREVIEW_REQUIRED不是hash。复制“修正后的清单”，请本角色通过已许可write_file原样保存到原路径，或由主人自行保存；再次预览到hash一致。不要让模型猜摘要或增加Python/Shell许可。“填入验证清单”只填写实际路径与当前清单SHA，不代表信任代码。文件改变后重新核对和验证。

## 显式保存MCP及来源许可

在MCP设置明确选择实际存在的绝对Python文件；参数逐项为 `-I` 和产物的绝对source.py路径，cwd为产物目录，明确勾选当前角色。服务ID与source-draft.json一致，默认authored_source；环境秘密只由主人在凭据UI填写，草稿仅记录key/ref名称。保存服务不等于启用自主来源。

回预览填写实际保存的服务ID，得到绑定当前config/grant revision和声明catalog定义hash的disabled来源草稿。实际握手必须一致，否则验证/执行拒绝。点击“填入来源许可草稿”，核对每项get/poll/ACK和事件域；同ID不会自动覆盖，更新时手动核对原许可。最多8项。明确将需要的来源改为enabled并保存；A01/Drift仍可关闭。

## 验证并激活

选择已保存来源、实际清单SHA并勾选信任，原autonomy_verify运行一次最长120秒验证。固定产物与解释器被复制到无凭据临时artifact/state/home，丢弃生产后缀、环境和vault；`--meido-contract-test <ownedStateRoot>`才暴露随机seed/clock测试控制，不读或清理正常state。

meido-source-author-v2按当前channel验证稳定数据、未ACK重读和进程重建、empty/error区别；content检查正文关联，context检查背景数据。alert/content另验证接受ACK后丢回执、原回执持久、同ackId不延长绝对expiry、有限到期和永久记录。纯context无需无关的alert/content/poll/ACK操作。

真实进程、stdio与临时目录全部收尾后才发布proof。取消、主人抢占、变版或清理未确认不能伪报成功；原nonce/key及清理责任保留，不能换请求重开代码。验证是受信同账号代码执行，不是OS沙箱。

事件来源通过后点击“使用验证结果”只填入ACK proof，仍需显式保存；context只显示读取证明，无ACK重试资格。旧v1仅表示原ACK保证，旧ttl_hours来源最多单次ACK，未知回执不重试。验证不自动授予真实账号事务、开启模型或发送。

## 查看后续消费

另行选择主动模型、桌面目标及有限操作许可并开启A01。首次合格判断等待30分钟和主人回合结束10分钟；周期判断还等待本次poll终态、owned收尾和Runtime结果ACK。下个真实clock消费同一产物，不能另建来源绕过失败。

最近运行区区分无事件、读取失败、skip与sent；sent须有原桌面消息收据，ACK区显示真实状态/尝试数/原期限。成功content为168h、暂缓24h、明确丢弃720h，引用alert永久，context不ACK。只有匹配当前完整binding/hash的绝对ACK proof允许既定三次ordinal及10分钟截止，重试不延长expiry、不重做发送。

重建应用和stdio后，ACK仍在原期限内抑制，未ACK仍返回。本地协议通过不代表真实第三方数据、真实账号或付费模型已验收。
