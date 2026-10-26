# 落地页发布

状态：当前

事实来源：`.github/workflows/pages.yml`、`vite.landing.config.ts`、`web/src/landing/`、Issue #254。
最后核验：2026-10-26。首次线上发布需发布配置合入 `main` 后完成。

## 地址与发布范围

GitHub Pages 的项目地址为 <https://sawaki-zexi.github.io/meido/>。
原有 Sites 地址 <https://meido-landing.swiftsnipe1.chatgpt.site> 继续保留，两个平台分别发布。

GitHub Pages 只发布静态介绍页。构建入口直接加载 `LandingPage`，不加载角色应用，
也不发布本地后端、运行数据或模型密钥。页面与主应用共用介绍文案、组件和素材。

## 自动与手动发布

仓库 Settings → Pages 的 Source 选择 **GitHub Actions**。
落地页相关代码、素材或构建配置合入 `main` 后，`Landing Pages` 工作流先运行落地页测试、
类型检查和独立构建，再发布 `web/dist-landing/`。PR 只验证；部署权限只给 `main` 的发布任务。

需要重新发布时，在 Actions → Landing Pages → Run workflow 中选择 `main`。
工作流成功后，通过发布任务的环境链接访问页面。失败时查看对应任务日志；构建失败不会替换线上页面。

工作流从 GitHub Pages 设置读取发布基路径。图片、立绘、音乐、脚本和样式使用同一基路径，
因此项目地址 `/meido/` 与根路径 `/` 都受支持。

## 本地验证

在独立开发工作区安装依赖后运行：

```text
npm run test:web -- web/src/landing/LandingPage.test.tsx
npm run build:landing -- --base /meido/
```

产物位于 `web/dist-landing/`，不提交到 Git。检查产物时使用匹配 `/meido/` 的静态预览路径；
不要直接双击 HTML，也不要把只供本机运行的 `/app` 当作 Pages 发布入口。

Sites 不会随 GitHub Pages 工作流同步；更新 Sites 时仍需单独发布。
