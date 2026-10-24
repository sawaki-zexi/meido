import type { AutonomyConfiguration } from '../../api/autonomy';
import type { ModelConfiguration } from '../../api/types';
import { Notice, Placeholder } from '../../ui/Status';
import { useDriftGrants } from './useDriftGrants';

export function DriftSettings({ roleId, config, models, onChange }: {
  roleId: string; config: AutonomyConfiguration; models: ModelConfiguration[]; onChange: (changes: Partial<AutonomyConfiguration>) => void;
}) {
  const grants = useDriftGrants(roleId, config, onChange);
  return <><section className="settings-section">
    <h3>Drift</h3>
    <fieldset aria-label="Drift 独立设置">
    <label className="field checkbox-field"><input type="checkbox" aria-label="开启 Drift" checked={config.driftEnabled}
      onChange={(event) => onChange({ driftEnabled: event.target.checked, consentRevision: 0 })} />开启 Drift</label>
    <label className="field"><span>Drift 模型连接</span><select aria-label="Drift 模型连接" value={config.driftModelConfigurationId ?? ''}
      onChange={(event) => onChange({ driftModelConfigurationId: event.target.value || null, consentRevision: 0 })}>
      <option value="">请选择已保存的 Drift 模型连接</option>
      {config.driftModelConfigurationId && !models.some((model) => model.id === config.driftModelConfigurationId)
        && <option value={config.driftModelConfigurationId} disabled>原 Drift 连接已不存在</option>}
      {models.map((model) => <option key={model.id} value={model.id}>{model.provider} · {model.model}（{model.id}）</option>)}
    </select></label>
    <p className="field-hint">空闲时执行获准任务，每天最多4次、间隔至少3小时。</p>
    </fieldset>
    </section>
    <details className="settings-disclosure">
    <summary><span>技能创作与工具许可</span></summary>
    <div>
    <label className="field checkbox-field"><input type="checkbox" checked={config.driftAuthoring.enabled}
      onChange={(event) => grants.author({ enabled: event.target.checked })} />允许创建或更新 Drift 技能</label>
    <p className="field-hint">在聊天中使用 $create-drift-skill 创建技能；声明的依赖仍需单独授予执行许可。</p>
    {config.driftAuthoring.scopeId && <p className="field-hint">作者范围：{config.driftAuthoring.scopeId} · 版本 {config.driftAuthoring.revision}</p>}
    <label className="field checkbox-field"><input type="checkbox" checked={config.driftAuthoring.allowMcp}
      onChange={(event) => grants.author({ allowMcp: event.target.checked })} />作者可声明 MCP 依赖</label>
    <label className="field checkbox-field"><input type="checkbox" checked={config.driftAuthoring.allowShell}
      onChange={(event) => grants.author({ allowShell: event.target.checked })} />作者可声明受限 Shell 依赖</label>
    <label className="field checkbox-field"><input type="checkbox" checked={config.driftAuthoring.allowAutonomousAuthoring}
      onChange={(event) => grants.author({ allowAutonomousAuthoring: event.target.checked })} />允许在自主运行中创作 Drift 技能</label>
    <button type="button" className="settings-inline-button" aria-expanded={grants.open} onClick={grants.toggle}>配置 Drift 工具许可</button>
    {grants.open && <section className="settings-permission-list" aria-label="Drift 声明与执行许可">
      <p className="field-hint">目录来自本地缓存；保存配置不会执行工具。</p>
      {grants.busy && <Placeholder loading>正在读取 Drift 许可目录…</Placeholder>}
      {grants.error && <Notice onDismiss={grants.dismissError}>{grants.error}</Notice>}
      <button type="button" disabled={grants.busy} onClick={() => void grants.reload()}>刷新 Drift 许可目录</button>
      {grants.options && <>
        <fieldset><legend>作者可声明的本地工具</legend>
          {[...new Set([...grants.options.tools.map((item) => item.name), ...config.driftAuthoring.allowedTools])].map((name) =>
            <label className="field checkbox-field" key={name}><input type="checkbox" aria-label={`作者可声明 ${name}`}
              checked={config.driftAuthoring.allowedTools.includes(name)} disabled={!config.driftAuthoring.allowedTools.includes(name) && config.driftAuthoring.allowedTools.length >= 128}
              onChange={(event) => grants.authorTool(name, event.target.checked)} />{name}</label>)}
        </fieldset>
        <fieldset><legend>Drift 实际本地工具许可</legend>
          {[...new Set([...grants.options.tools.map((item) => item.name), ...config.driftToolGrants])].map((name) => {
            const option = grants.options!.tools.find((item) => item.name === name);
            const selected = config.driftToolGrants.includes(name);
            return <label className="field checkbox-field" key={name}><input type="checkbox" aria-label={`执行许可 ${name}`} checked={selected}
              disabled={!selected && (!option?.available || config.driftToolGrants.length >= 128)} onChange={(event) => grants.tool(name, event.target.checked)} />{name}
              {!option?.available && <small>{option?.reason || '请先在角色能力设置中授权该工具'}</small>}</label>;
          })}
        </fieldset>
        <fieldset><legend>宿主只读资源</legend>
          {grants.options.resources.map((resource) => <div key={resource.id}>
            <label className="field checkbox-field"><input type="checkbox" aria-label={`作者可声明资源 ${resource.name}`} checked={config.driftAuthoring.allowedResourceIds.includes(resource.id)}
              onChange={(event) => grants.authorResource(resource.id, event.target.checked)} />作者可声明资源 {resource.name}</label>
            <label className="field checkbox-field"><input type="checkbox" aria-label={`只读资源 ${resource.name}`} checked={config.driftReadResourceIds.includes(resource.id)}
              disabled={!resource.available && !config.driftReadResourceIds.includes(resource.id)} onChange={(event) => grants.resource(resource.id, event.target.checked)} />只读资源 {resource.name}</label>
            {!resource.available && <p>{resource.reason || '资源当前不可用'}</p>}
          </div>)}
          {grants.options.resources.length === 0 && <p>当前没有可选宿主只读资源。</p>}
        </fieldset>
        <fieldset><legend>已核验的只读 Shell 预设</legend>
          {grants.options.shellRules.map((rule) => <label className="field checkbox-field" key={rule.id}>
            <input type="checkbox" aria-label={`Shell 许可 ${rule.id}`} checked={config.shellRuleIds.includes(rule.id)}
              disabled={!rule.available && !config.shellRuleIds.includes(rule.id)} onChange={(event) => grants.shell(rule.id, event.target.checked)} />{rule.description}
            {!rule.available && <small>{rule.reason || '请先配置角色 Shell 权限与命令白名单'}</small>}</label>)}
          <p>只运行宿主固定的只读预设；此处不接收命令、环境变量或可执行路径。</p>
        </fieldset>
        <fieldset><legend>精确 MCP 工具许可</legend>
          {grants.options.mcpServers.map((server) => {
            const saved = config.driftMcpGrants.find((grant) => grant.serverId === server.serverId);
            const stale = Boolean(saved && (saved.configRevision !== server.configRevision || saved.grantRevision !== server.grantRevision
              || saved.operations.some((operation) => !server.operations.some((item) => item.name === operation.name && item.definitionHash === operation.definitionHash))));
            return <section key={server.serverId} aria-label={`MCP 服务 ${server.name}`}>
              <h4>{server.name}（{server.serverId}）</h4><p>风险：{server.risk} · 配置 {server.configRevision} · 许可 {server.grantRevision}</p>
              <p>{server.status === 'observed' ? '目录已观察；实际运行仍需握手校验，不代表服务在线。' : server.reason || '尚无当前目录，请在普通主人聊天通过 mcp_add 连接已保存服务后刷新。'}</p>
              {stale && <p>已保存的工具定义或许可版本已变化，请移除旧许可后重新选择。</p>}
              {server.operations.map((operation) => <label className="field checkbox-field" key={operation.name}>
                <input type="checkbox" aria-label={`MCP 许可 ${server.serverId} ${operation.name}`}
                  checked={!stale && Boolean(saved?.operations.some((item) => item.name === operation.name && item.definitionHash === operation.definitionHash))}
                  disabled={server.status !== 'observed' || stale || (!saved && config.driftMcpGrants.length >= 8)}
                  onChange={(event) => grants.mcp(server, operation.name, event.target.checked)} />{operation.description} · {operation.name}
                <small>定义 SHA256：{operation.definitionHash}</small></label>)}
              {saved && <button type="button" onClick={() => grants.removeMcp(server.serverId)}>移除 MCP 许可 {server.serverId}</button>}
            </section>;
          })}
          {config.driftMcpGrants.filter((grant) => !grants.options!.mcpServers.some((server) => server.serverId === grant.serverId)).map((grant) =>
            <p key={grant.serverId}>已保存服务 {grant.serverId} 当前不可用。<button type="button" onClick={() => grants.removeMcp(grant.serverId)}>移除 MCP 许可 {grant.serverId}</button></p>)}
          {grants.options.mcpServers.length === 0 && <p>尚无可用 MCP 目录，请在普通主人聊天通过 mcp_add 连接已保存服务后刷新。</p>}
        </fieldset>
      </>}
      <label className="field"><span>获准 Drift 附件 ID（每行一个）</span><textarea aria-label="获准 Drift 附件 ID（每行一个）" value={grants.attachmentText}
        onChange={(event) => grants.attachments(event.target.value)} /><small>最多16个当前角色已有公共附件；保存时由宿主验证归属。</small></label>
    </section>}
    </div>
    </details>
  </>;
}
