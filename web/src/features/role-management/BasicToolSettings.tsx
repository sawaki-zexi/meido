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
] as const;

export function BasicToolSettings({ draft, onChange, disabled }: { draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled: boolean }) {
  const setEnabled = (name: string, enabled: boolean) => onChange({
    ...draft,
    otherEnabledTools: enabled
      ? draft.otherEnabledTools.includes(name) ? draft.otherEnabledTools : [...draft.otherEnabledTools, name]
      : draft.otherEnabledTools.filter((item) => item !== name),
  });
  return <fieldset>
    <legend>基础工具授权</legend>
    <p className="field-hint">逐项选择此角色可用的工具，保存后生效。授权不代表依赖或服务已就绪；保存后可在工具目录检查实际状态。</p>
    {groups.map((group) => <section key={group.title} aria-label={group.title}>
      <h4>{group.title}</h4>
      <p className="field-hint">{group.hint}</p>
      {group.tools.map(([name, label]) => <label className="field checkbox-field" key={name}>
        <input type="checkbox" aria-label={`${label}（${name}）`} checked={draft.otherEnabledTools.includes(name)} disabled={disabled} onChange={(event) => setEnabled(name, event.target.checked)} />
        <span>{label}（{name}）</span>
      </label>)}
    </section>)}
  </fieldset>;
}
