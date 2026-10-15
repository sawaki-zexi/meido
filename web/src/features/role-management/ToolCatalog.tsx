import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { RoleToolCatalog } from "../../api/types";
import "./tool-catalog.css";

export function ToolCatalog({ roleId, hasUnsavedChanges = false }: { roleId: string; hasUnsavedChanges?: boolean }) {
  const [catalog, setCatalog] = useState<RoleToolCatalog | null>(null);
  const [opened, setOpened] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);
  const refresh = async () => {
    const request = ++generation.current;
    setOpened(true); setLoading(true); setError("");
    try {
      const result = await api<RoleToolCatalog>(`/api/roles/${encodeURIComponent(roleId)}/tools`);
      if (request === generation.current) setCatalog(result);
    } catch (cause) {
      if (request === generation.current) setError(errorMessage(cause, "无法加载工具目录"));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  };
  return <section className="tool-catalog" aria-label="工具目录">
    {hasUnsavedChanges && <p role="status" className="field-hint">工具设置尚未保存；目录仍使用已保存的配置。</p>}
    <button type="button" className="ghost" disabled={loading} onClick={() => void refresh()}>{loading ? "检查工具配置中…" : opened ? "刷新工具目录" : "查看实际工具"}</button>
    {opened && <>
      <p className="field-hint">按已保存配置预览下一次桌面对话；更改配置后请先保存再刷新。当前运行开始时授权冻结，撤销许可仍可阻止执行；按需发现只会追加已授权工具的模型可见性版本。</p>
      {error && <p role="alert">{error}</p>}
      {catalog && <>
        <ul aria-label="实际工具">{catalog.tools.map((tool) => <li key={tool.name}>
          <strong>{tool.name}</strong>
          <p>{tool.description}</p>
          <div className="tool-catalog-states"><span>{tool.enabled ? "已启用" : "未启用"}</span><span>{tool.authorized ? "策略允许" : "策略未允许"}</span><span>{tool.modelVisible === null ? "模型可见性待确认" : tool.modelVisible ? "传给模型" : "不传给模型"}</span><span>{tool.dependency.status === "ready" ? "静态依赖检查通过" : tool.dependency.status === "unavailable" ? "依赖不可用" : "依赖待验证"}</span></div>
          <small>{tool.reason}</small><small>{tool.dependency.reason}</small>
          {tool.source && <small>来源：{tool.source}{tool.version ? ` · ${tool.version}` : ""}</small>}
        </li>)}</ul>
        <details><summary>已注册插件（{catalog.plugins.length}）</summary><ul aria-label="已注册插件">{catalog.plugins.map((plugin) => <li key={plugin.id}><strong>{plugin.id}</strong><span>{plugin.enabled ? "已启用" : "未启用"} · {plugin.authorized ? "已授权" : "未授权"}</span><small>{plugin.reason}</small></li>)}</ul></details>
      </>}
    </>}
  </section>;
}
