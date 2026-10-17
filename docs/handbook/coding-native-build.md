# 构建和核验 owned Herdr

状态：当前本地构建流程；真实账号核验另批。最后核验：2026-10-16。事实来源：[构建脚本](../../scripts/coding-native/build_owned.py)、[锁](../../third_party/herdr-owned/manifest.json)、[资源验证](../../backend/app/coding_tasks/native_build.py)、[核验流程](../../backend/app/coding_tasks/probes.py)。

## 准备独立构建目录

固定源为 herdrdev/herdr 的 `7e26bad94044eba205033111a0ce8033ede7e24f`，owned版本0.9.4-meido.1。使用私有 Rust1.96.1/CARGO_HOME/RUSTUP_HOME、Zig0.16.0/cache；已安装 MSVC14.42.34433 与SDK10.0.22621.0。不要安装到全局 PATH、覆盖用户配置或复用其它 agent 正在写入的工具链/cache。

准备官方 ConPTY `Microsoft.Windows.Console.ConPTY 1.24.260710001` nupkg，按[ConPTY锁](../../third_party/herdr-owned/conpty.lock.json)校验下载及包内文件。脚本必须收到已下载包，不从主人程序运行时隐式下载。

```powershell
python scripts/coding-native/build_owned.py `
  --workspace D:/isolated-herdr-build `
  --cargo D:/isolated-tools/cargo/bin/cargo.exe `
  --cargo-home D:/isolated-tools/cargo `
  --rustup-home D:/isolated-tools/rustup `
  --zig D:/isolated-tools/zig/zig.exe `
  --conpty-package D:/isolated-tools/conpty.nupkg `
  --output D:/isolated-herdr-bundle
```

workspace/source 与 output 必须是新目录；脚本不覆盖已有构建。它拉取固定公开源码，验证/应用唯一补丁，运行 cargo build --locked -j2，暂存 ConPTY、上游许可和 meido-owned-build.json。保留整个 bundle，不能只复制 exe。运行 Meido 的源码发行还需保留 third_party/herdr-owned 的锁及许可资源。

当前接受的是实际验收过的 **debug** 二进制 SHA-256 `445b03a54b314fffc6720b1229b3f6dda43bc42c28ddb4ec91c0f5144d25ce67`。未宣称不同构建路径可位级复现；新产物哈希不同会明确拒绝暂存，须维护者对该准确产物重跑原生验收并审查更新第一方锁，不能只改旁边的 manifest 令它自行通过。首次 Zig std 构建曾失败；隔离缓存及 -j2 后真实编译成功，不把失败删改成成功。

## 配置产品

1. 打开设置→编程委托，登记整个固定 bundle 的 herdr.exe 路径与哈希，点击“核验宿主”。只验证本地固定能力和资源，不调用模型。
2. 登记主人信任的精确 Git 根、允许角色和固定验证命令；程序首项必须绝对路径，cwd只能在将创建的 worktree内。首版不批准 fetch、commit 或发布。
3. 登记专用 Codex0.159.3 或 ClaudeCode2.1.283 程序及SHA、固定模型、明确认证目录和允许角色。当前会话未授权读取真实认证；维护者应先取得具体账号/模型/核验任务费用许可，再在该目录准备认证。保存不会搜索桌面认证或调用模型。
4. 核验前确认页面显示的 profile、version/model和固定宿主。明确勾选首次+继续两个任务回合的消费许可后点击对应“核验”。内部可能多次请求模型，整个核验最多600秒。只打开设置或普通保存不会启动核验。
5. 刷新核验状态。宿主验证首次、同一精确 native UUID继续、实际 worktree/固定验证、原生拒绝事件和全部 Job 收尾。失败会给具体缺项并保持不可用；不存在自行填写 verified 的入口。合成CLI证据不能替代真实CLI核验。
6. 全局开启后，在角色资料选择仓库/profile/default，启用编程和工具发现并保存。重新打开检查许可；主人当前对话可要求开始、查询、继续或取消，聊天“编程任务”可查看后台状态和回执。

改认证或程序/profile后重新保存并核验；撤销仓库/CLI或关闭角色会使旧授权失效。cleanup_failed 应先重试收尾，不能换任务绕过。Job不能可靠观察时保持冻结，勿将“runtime已结束”当作资源消失。删除角色保留未提交worktree及所有权记录，由主人另行决定清理。

## 常见阻塞

缺固定bundle/哈希、非Windows、未授仓库、未选择唯一profile、认证摘要变化或未取得原生证据，都会保持不可用。项目 .codex/.claude/.mcp配置当前不支持，原仓库与worktree均检查到各自Git项目根；不要为通过核验读取或搬入桌面的全局配置。程序不是同账号OS安全沙箱，只对主人信任的仓库开放。真实CLI可能因版本/账户/实际权限模型而不支持当前模板；此时 blocked 是验收结果，需进一步查明并审查修正，不能用fixture成功宣称真实可用。
