# 表情包 Skill 的本地媒体链验收

状态：当前 / 现有实现的本地组合证据，未改生产或仓库测试。最后核验：2026-10-16，准确干净基点e71145aade2644d6b56225fddf3673cbf236cabd。事实来源：[S04正文](../../backend/app/builtin_skills/meme-manage/SKILL.md)、[文件工具](../../backend/app/agent_runtime/file_tools.py)、[附件入口](../../backend/app/chat_attachments.py)、[桌面投递](../../backend/app/desktop_push.py)、[历史回放](../../backend/app/session_store.py)；相关既有测试为[Skill HTTP](../../tests/python/test_builtin_skills_api.py)、[文件HTTP](../../tests/python/test_file_tools_api.py)、[桌面推送HTTP](../../tests/python/test_desktop_push_api.py)。

## 实际链路

外部组合夹具1 passed/9.52秒。主人只给新角色read_file/list_dir/write_file、独立message_push与唯一xcopy.exe白名单，显式激活meme-manage；Skill没有额外授予edit_file、其他命令或发送权。

1. 经真实附件HTTP上传自建48×32、422字节PNG，登记为本角色媒体ID；首个对话没有附图块。
2. 原文件工具读取上传目录与源图、写表情manifest和类别；受管Shell实际二进制复制到角色memes目录，exit0，原角色/sourceRun/toolCall保留。复制前目标不存在，复制后与源字节一致。
3. 再用原read_file读取目标，真实PNG image_url只进入下一provider请求；两次视觉传输分别位于第3/6次请求。scripted adapter只证明字节与协议传输，不能证明真实模型识图。
4. message_push复用已登记媒体ID写入原桌面消息，公开metadata及实际下载字节正确。重建SessionStore、SessionManager和RuntimeManager后，下一主人输入不含ID；provider从真实历史“已推送附件”标签找回ID，再调用message_push成功。
5. 第三回合将原始memes路径充当媒体ID，schema实际拒绝，无第三条push。只读SQLite核对3个主人run均completed、恰2条desktop_push且无新增run，原sourceRun/toolCall对应实际工具执行。

总计11次本地scripted provider请求、3个真实HTTP主人回合；没有渠道账号。PNG SHA256为7ff431419841e7cd091f71d71a914229ab99718b648d8d258e6abbca284d4fb9。历史仅保留安全名称/ID及实际回执，临时read_file图像字节不自动重放，不写入公开历史。

## 边界与证据

此链仅证明“上传登记角色媒体ID→不变字节复制→复用同ID发送”。任意新memes路径不会自动注册附件，需要先经已有上传入口登记；不能把此证据扩大为任意路径发送。重建对象不是完整进程重启，也没有真实桌面DOM截图或远端模型验收。#179的Drift文本循环不具有本链的视觉或二进制复制资格。

验收前后HEAD、clean状态及9份相关生产/Skill SHA完全相同。内外localhost guard覆盖main导入到全部owner退出，非本地尝试0；TaskRuntime active0、main关闭后新增活线程0，最终本轮Python/xcopy进程0。仅用自建图片、本地模型夹具和临时角色；未读取用户图片、真实凭据、模型或平台网络，未发生费用。

协调者保留外部probe/runner、XML、输入快照、工具/视觉/媒体及SQLite回执证据。最终probe SHA256为a97d30e6766e7cbc71674a5ef990c908fa635700fd3a3a0e8ca590ad4e848588，runner SHA256为31925ceae401d0afb296412c77980e2f7ed86058f0bea19acbd98c69c255460e。首轮只因外部fixture将xcopy argv[0]写为正斜杠而返回参数错误；同源/目标加只读/L比较后，仅改为原生Windows路径，原失败XML与旧probe保留，没有改生产或白名单。追加只读SQL取证第一次误用不存在的created_at字段，改读真实started_at；属于取证装配错误，不算产品失败或通过。
