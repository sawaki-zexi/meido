import type { RoleDraft } from "./draft";

const groups = [
  { title: "工作区读取", hint: "只读取当前角色获准的文件和目录。", tools: [
    ["read_file", "读取文件"], ["list_dir", "列出目录"],
  ] },
  { title: "工作区修改", hint: "允许在当前角色获准的工作区写入或修改文件。", tools: [
    ["write_file", "写入文件"], ["edit_file", "编辑文件"],
  ] },
  { title: "联网查询", hint: "通过宿主已配置的服务搜索和读取网页；服务或网络关闭时仍不可用。", tools: [
    ["web_search", "搜索网页"], ["web_fetch", "抓取网页"],
  ] },
  { title: "消息原文", hint: "只访问当前角色和发言者获准读取的消息范围。", tools: [
    ["search_messages", "搜索消息原文"], ["fetch_messages", "读取消息原文"],
  ] },
  { title: "显式记忆维护", hint: "保存或撤销角色记忆；全局记忆关闭或来源未获授权时不可用。", tools: [
    ["memorize", "记住信息"], ["forget_memory", "撤销记忆"],
  ] },
  { title: "按需发现", hint: "只发现并激活已授权的候选工具，不能增加权限。", tools: [
    ["tool_search", "按需发现工具"],
  ] },
  { title: "桌面对话", hint: "将文本、图片或文件推送到当前角色自己的桌面对话。", tools: [
    ["message_push", "桌面推送"],
  ] },
  { title: "时间任务", hint: "按主人要求创建、查询和取消时间任务。关闭创建授权会停止计划执行。", tools: [
    ["schedule", "创建时间任务"], ["list_schedules", "查询时间任务"], ["cancel_schedule", "取消时间任务"],
  ] },
] as const;

export function BasicToolSettings(_props: { draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean }) {
  return <fieldset>
    <legend>默认内置工具</legend>
    <p className="field-hint">基础工具默认提供，无需逐项配置。文件、网络、消息、记忆、Shell、后台任务、子任务和时间任务随角色可用；宿主服务与来源限制仍生效。</p>
    {groups.map((group) => <section key={group.title} aria-label={group.title}>
      <h4>{group.title}</h4>
      <p className="field-hint">{group.hint}</p>
      <p>{group.tools.map(([name, label]) => `${label}（${name}）`).join('、')}</p>
    </section>)}
  </fieldset>;
}
