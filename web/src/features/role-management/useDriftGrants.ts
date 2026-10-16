import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { autonomy, type AutonomyConfiguration, type DriftAuthoring, type DriftGrantOptions } from '../../api/autonomy';

const select = (values: string[], value: string, enabled: boolean) => enabled ? [...new Set([...values, value])] : values.filter((item) => item !== value);
export function useDriftGrants(roleId: string, config: AutonomyConfiguration, onChange: (changes: Partial<AutonomyConfiguration>) => void) {
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<DriftGrantOptions | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [attachmentText, setAttachmentText] = useState(config.driftAttachmentIds.join('\n'));
  const generation = useRef(0);
  useEffect(() => { generation.current++; setOpen(false); setOptions(null); setBusy(false); setError(''); return () => { generation.current++; }; }, [roleId]);
  useEffect(() => { setAttachmentText(config.driftAttachmentIds.join('\n')); }, [roleId, config.revision]);
  const reload = async () => {
    const current = ++generation.current;
    setBusy(true); setError(''); setOptions(null);
    try { const result = await autonomy.driftGrantOptions(roleId); if (current === generation.current) setOptions(result); }
    catch (cause) { if (current === generation.current) setError(errorMessage(cause, 'Drift 许可目录暂不可读')); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const patch = (changes: Partial<AutonomyConfiguration>) => onChange({ ...changes, consentRevision: 0 });
  const author = (changes: Partial<DriftAuthoring>) => patch({ driftAuthoring: { ...config.driftAuthoring, ...changes } });
  const mcp = (server: DriftGrantOptions['mcpServers'][number], name: string, checked: boolean) => {
    if (server.status !== 'observed') return;
    const old = config.driftMcpGrants.find((item) => item.serverId === server.serverId);
    if (old && (old.configRevision !== server.configRevision || old.grantRevision !== server.grantRevision)) return;
    const operation = server.operations.find((item) => item.name === name);
    if (!operation) return;
    const operations = checked ? [...(old?.operations ?? []).filter((item) => item.name !== name), { name, definitionHash: operation.definitionHash }]
      : (old?.operations ?? []).filter((item) => item.name !== name);
    const remaining = config.driftMcpGrants.filter((item) => item.serverId !== server.serverId);
    patch({ driftMcpGrants: operations.length ? [...remaining, { serverId: server.serverId, configRevision: server.configRevision,
      grantRevision: server.grantRevision, risk: server.risk, operations }] : remaining });
  };
  return { open, options, busy, error, attachmentText, dismissError: () => setError(''), reload,
    toggle: () => { setOpen(!open); if (!open) void reload(); }, author,
    authorTool: (name: string, checked: boolean) => author({ allowedTools: select(config.driftAuthoring.allowedTools, name, checked) }),
    authorResource: (id: string, checked: boolean) => author({ allowedResourceIds: select(config.driftAuthoring.allowedResourceIds, id, checked) }),
    tool: (name: string, checked: boolean) => patch({ driftToolGrants: select(config.driftToolGrants, name, checked) }),
    resource: (id: string, checked: boolean) => patch({ driftReadResourceIds: select(config.driftReadResourceIds, id, checked) }),
    shell: (id: string, checked: boolean) => patch({ shellRuleIds: select(config.shellRuleIds, id, checked) }),
    attachments: (value: string) => { setAttachmentText(value); patch({ driftAttachmentIds: [...new Set(value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean))] }); },
    mcp, removeMcp: (serverId: string) => patch({ driftMcpGrants: config.driftMcpGrants.filter((item) => item.serverId !== serverId) }) };
}
