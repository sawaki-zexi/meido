import { useEffect, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { Role, RoleToolCatalog } from "../../api/types";
import { IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";

type Props = { role: Role; onOpenSettings?: () => void };

/** Read-only role capability summary. Editing remains in Settings → Plugins. */
export function RoleCapabilitiesPanel({ role, onOpenSettings }: Props) {
  const [catalog, setCatalog] = useState<RoleToolCatalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError("");
    void api<RoleToolCatalog>(`/api/roles/${encodeURIComponent(role.id)}/tools`)
      .then(value => { if (current) setCatalog(value); })
      .catch(cause => { if (current) setError(errorMessage(cause, "无法读取角色能力")); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [role.id, revision]);

  if (loading) return <section className="role-capabilities" aria-label="角色能力"><Placeholder loading>正在读取能力…</Placeholder></section>;
  return <section className="role-capabilities" aria-label="角色能力">
    <header className="role-section-heading">
      <div><h2>能力</h2><p>查看当前角色实际可用的工具和插件。权限配置在设置中管理。</p></div>
      <div className="role-section-actions">
        <IconButton icon="refresh" label="刷新能力" onClick={() => setRevision(value => value + 1)} />
        {onOpenSettings && <button type="button" className="ghost" onClick={onOpenSettings}>打开设置</button>}
      </div>
    </header>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {catalog && <>
      <div className="role-capability-summary"><strong>{catalog.tools.filter(item => item.enabled && item.authorized).length}</strong><span>项已授权能力</span><small>{catalog.tools.length} 项已登记工具</small></div>
      <ul className="role-capability-list" aria-label="可用工具">
        {catalog.tools.map(tool => <li key={tool.name} className={tool.enabled && tool.authorized ? "is-enabled" : ""}>
          <span className="role-capability-mark" aria-hidden="true">{tool.enabled && tool.authorized ? "✓" : "—"}</span>
          <span className="role-capability-copy"><strong>{tool.name}</strong><small>{tool.description}</small></span>
          <span className="role-capability-status">{tool.enabled && tool.authorized ? "已启用" : tool.reason || "未启用"}</span>
        </li>)}
      </ul>
    </>}
  </section>;
}
