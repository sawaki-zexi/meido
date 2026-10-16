import type { AutonomyConfiguration } from '../../api/autonomy';
import { Notice, Placeholder } from '../../ui/Status';
import { useDriftSkills } from './useDriftSkills';

const statusLabels = { untrusted: '尚未核验作者来源或采用', invalid: '产物校验未通过',
  pending_dependency: '来源已核验，等待执行依赖', available: '本地许可与可信产物齐备' };
const messageLabels = { sent: '已发送，有真实投递回执', silent: '静默完成，未消耗发送机会', failed: '发送确定失败', uncertain: '发送结果尚未确认' };
export function DriftSkillsPanel({ roleId, config, hasUnsavedChanges }: {
  roleId: string; config: AutonomyConfiguration; hasUnsavedChanges: boolean;
}) {
  const state = useDriftSkills(roleId, config, hasUnsavedChanges);
  return <section aria-label="Drift 长期技能">
    <button type="button" aria-expanded={state.open} onClick={state.toggle}>查看 Drift 技能</button>
    {state.open && <>
      <h4>Drift 长期技能</h4>
      <p>在普通聊天中显式使用 $create-drift-skill 准备技能。缺依赖时保留同一 hash，补齐许可并刷新即可，无需重新生成。可用状态不代表 MCP 服务在线，每次运行仍需真实握手。</p>
      {state.error && <Notice onDismiss={state.dismissError}>{state.error}</Notice>}
      {state.message && <Notice tone="success" onDismiss={state.dismissMessage}>{state.message}</Notice>}
      {state.busy && <Placeholder loading>正在读取或核对 Drift 技能…</Placeholder>}
      <button type="button" disabled={state.busy} onClick={() => void state.reload()}>刷新 Drift 技能状态</button>
      {state.skills?.length === 0 && <Placeholder>暂无 Drift 技能，请先使用 $create-drift-skill 准备。</Placeholder>}
      {state.skills?.map((skill) => <article key={skill.id} aria-label={`Drift 技能 ${skill.id}`}>
        <h4>{skill.id}{skill.version ? ` · ${skill.version}` : ''}</h4>
        <p>{skill.description}</p><p>{statusLabels[skill.status]}</p>
        {skill.bundleSha256 && <p>技能 SHA256：{skill.bundleSha256}</p>}
        {skill.reason && <p>{skill.reason}</p>}
        {skill.missingTools?.length ? <p>缺少本地工具许可：{skill.missingTools.join('、')}；请配置 Drift 工具许可。</p> : null}
        {skill.missingMcp?.length ? <p>缺少 MCP 许可：{skill.missingMcp.join('、')}；请观察目录并选择精确工具定义。</p> : null}
        {skill.missingResources?.length ? <p>缺少只读资源许可：{skill.missingResources.join('、')}。</p> : null}
        {skill.missingShellRules?.length ? <p>缺少 Shell 预设许可：{skill.missingShellRules.join('、')}；需先配置角色 Shell 白名单。</p> : null}
        {skill.provenance && <p>{skill.provenance.status === 'verified' ? '作者来源或主人采用已核验' : '尚无当前作者来源证明'}{skill.provenance.runId ? ` · 作者运行 ${skill.provenance.runId}` : ''}</p>}
        {skill.progress && <div aria-label={`${skill.id} 的持久进度`}>
          <p>已运行 {skill.progress.runCount} 次 · {skill.progress.oneLine}</p><p>下一步：{skill.progress.nextAction}</p>
          <p>最近消息结果：{messageLabels[skill.progress.messageResult]}</p>
          {skill.progress.projectionPending && <p role="status">进度已持久保存，工作文件投影待恢复；这不表示文件已经写入。</p>}
        </div>}
      </article>)}
      <fieldset aria-label="Drift 精确产物采用" disabled={state.busy}>
        <legend>预览并采用当前角色产物</legend>
        <p>仅只读本角色 drift/skills 文件，不执行技能或连接服务。先保存作者范围与配置，再预览；采用只登记当前精确产物。</p>
        {hasUnsavedChanges && <p role="status">自主草稿尚未保存，请先保存或重新读取，再预览并采用。</p>}
        <label className="field"><span>Drift 技能 ID</span><input aria-label="Drift 技能 ID" maxLength={64} value={state.skillId} disabled={Boolean(state.pendingAdoption)}
          onChange={(event) => state.changeSkillId(event.target.value)} placeholder="reading-notes" /></label>
        <button type="button" disabled={hasUnsavedChanges || Boolean(state.pendingAdoption) || !/^[a-z0-9][a-z0-9._-]{0,63}$/.test(state.skillId.trim())}
          onClick={() => void state.inspect()}>预览 Drift 技能产物</button>
        {state.preview && <>
          <p>产物 SHA256：{state.preview.bundleSha256}</p>
          <p>作者范围：{state.preview.scopeId || '尚未分配'} · 范围版本 {state.preview.scopeRevision} · 配置版本 {state.preview.configRevision}</p>
          <div aria-label="Drift 产物文件及 SHA256">{state.preview.files.map((file) => <p key={file.path}>{file.path} · {file.size} bytes · {file.sha256}</p>)}</div>
          <label className="field"><span>Drift 产物声明</span><textarea aria-label="Drift 产物声明" readOnly rows={6} value={JSON.stringify(state.preview.declaration, null, 2)} /></label>
          {state.preview.reason && <p>{state.preview.reason}</p>}
          {!config.driftAuthoring.enabled && <p>请先保存明确的作者范围许可。</p>}
          <label className="field checkbox-field"><input type="checkbox" checked={state.trusted} onChange={(event) => state.setTrusted(event.target.checked)} />我确认采用当前预览的精确产物及作者范围</label>
          {state.pendingAdoption && <p>采用结果尚未确认；重查保留原预览标识和版本，不会自动发起新的采用。</p>}
          <button type="button" disabled={!state.canAdopt} onClick={() => void state.adopt()}>{state.pendingAdoption ? '重查本次采用' : '采用当前 Drift 产物'}</button>
        </>}
      </fieldset>
    </>}
  </section>;
}
