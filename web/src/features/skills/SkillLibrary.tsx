import { useEffect, useRef, useState } from 'react';
import { skillApi, type SkillCatalog, type SkillDocument, type OrdinarySkill, type SkillPolicy, type SkillResource } from '../../api/skills';
import { errorMessage } from '../../api/client';
import './skills.css';

const sources: Record<string, string> = { role: '角色私有', shared: '主人公共库', project: '项目提供', builtin: '内置' };
const statuses = { available: '可使用', unavailable: '暂不可用', disabled: '已停用', shadowed: '被同名版本覆盖' };
const policies: Record<SkillPolicy, string> = { automatic: '按任务自动选择', manual: '仅手动指定', constant: '保持常驻指导' };
const legacyPolicies: Record<string, string> = { explicit: '手动指定', keyword: '关键词加载', role_default: '常驻指导', model: '自动选择' };
const template = '---\nname: my-method\ndescription: 说明何时适用、何时不适用；详细步骤放在正文。\n---\n# 方法\n\n输入：\n\n步骤：\n\n核验和失败处理：\n';

export function SkillLibrary({ roleId, roleName }: { roleId: string; roleName: string }) {
  const [catalog, setCatalog] = useState<SkillCatalog | null>(null);
  const [selected, setSelected] = useState<OrdinarySkill | null>(null);
  const [document, setDocument] = useState<SkillDocument | undefined>();
  const [content, setContent] = useState<string | null>(null);
  const [resources, setResources] = useState<SkillResource[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [preview, setPreview] = useState(false);
  const [confirm, setConfirm] = useState<'remove' | 'publish' | null>(null);
  const [reveal, setReveal] = useState(0);
  const editor = useRef<HTMLDivElement>(null);
  const alive = useRef(true);
  const currentRole = useRef(roleId);
  currentRole.current = roleId;
  const isCurrent = () => alive.current && currentRole.current === roleId;
  const unsaved = Boolean(document && content !== document.content || resources.length);
  const generation = useRef(0);
  const refresh = async () => {
    const value = await skillApi.list(roleId);
    if (isCurrent()) setCatalog(value);
    return value;
  };
  useEffect(() => {
    alive.current = true;
    setCatalog(null); setContent(null); setSelected(null); setDocument(undefined); setError(''); setNotice(''); setConfirm(null); setResources([]); setBusy(false); setPreview(false);
    void refresh().catch(cause => { if (isCurrent()) setError(errorMessage(cause, '技能读取失败')); });
    return () => { alive.current = false; generation.current++; };
  }, [roleId]);
  useEffect(() => {
    if (reveal && window.matchMedia?.('(max-width: 760px)').matches) {
      editor.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
      editor.current?.focus({ preventScroll: true });
    }
  }, [reveal]);
  const work = async (operation: () => Promise<void>) => {
    setError(''); setNotice(''); setBusy(true);
    try { await operation(); } catch (cause) { if (isCurrent()) setError(errorMessage(cause, '操作失败，请重试')); }
    finally { if (isCurrent()) setBusy(false); }
  };
  const open = (skill: OrdinarySkill) => void work(async () => {
    const token = ++generation.current;
    const value = await skillApi.document(roleId, skill.ref);
    if (!isCurrent() || token !== generation.current) return;
    setSelected(skill); setDocument(value); setContent(value.content); setResources([]); setConfirm(null);
    setReveal(value => value + 1);
  });
  const configure = (values: { enabled?: boolean; ref?: string; active?: boolean; policy?: SkillPolicy }) => void work(async () => {
    if (!catalog) return;
    await skillApi.settings(roleId, catalog.revision, values);
    await refresh();
    if (isCurrent()) { setPreview(false); setNotice(values.enabled === false ? '已回退旧触发方式，保存的技能和资源仍保留。' : '技能使用设置已保存。'); }
  });
  const importMarkdown = async (file: File) => {
    if (file.size > 256 * 1024) throw new Error('技能文件不能超过 256 KiB');
    const value = await file.text();
    if (!isCurrent()) return;
    generation.current++; setSelected(null); setDocument(undefined); setContent(value); setResources([]); setConfirm(null);
    setNotice('文件已读取；请检查内容并保存到当前角色。');
    setReveal(value => value + 1);
  };
  const addResources = async (files: File[]) => {
    const additions: SkillResource[] = [];
    for (const file of files) {
      if (file.size > 256 * 1024) throw new Error(`支持文件 ${file.name} 超过 256 KiB`);
      const data = new Uint8Array(await file.arrayBuffer());
      let binary = '';
      for (const byte of data) binary += String.fromCharCode(byte);
      additions.push({ path: `references/${file.name}`, content: btoa(binary), encoding: 'base64' });
    }
    if (isCurrent()) setResources(previous => [...previous, ...additions]);
  };
  return <section className="ordinary-skills" aria-label={`${roleName}的普通技能`}>
    <header className="skills-header"><div><p className="skills-eyebrow">可复用的方法</p><h2>技能库</h2>
      <p>让{roleName}按任务读取合适的方法。读取、执行与任务完成分别记录。</p></div>
      <button type="button" className="primary" disabled={busy} onClick={() => { generation.current++; setSelected(null); setDocument(undefined); setContent(template); setResources([]); setNotice(''); setError(''); setReveal(value => value + 1); }}>新建技能</button>
    </header>
    {error && <p role="alert" className="skills-error">{error}</p>}
    {notice && <p role="status" className="skills-notice">{notice}</p>}
    {!catalog && !error && <p>正在读取技能…</p>}
    {catalog && <>
      <div className="skills-discovery"><div><strong>{catalog.enabled ? '自主选择已启用' : '沿用旧触发方式'}</strong>
        <p>{catalog.enabled ? '根据任务查看目录，需要时才读正文；原有角色手动技能仍需主人指定，内置方法按迁移预览使用。' : '先预览变化，再仅为当前角色启用；随时可回退。'}</p></div>
        {catalog.enabled ? <button type="button" className="ghost" disabled={busy} onClick={() => configure({ enabled: false })}>回退旧触发方式</button>
          : <button type="button" className="ghost" disabled={busy} aria-expanded={preview} onClick={() => setPreview(value => !value)}>预览启用变化</button>}
      </div>
      {preview && <div className="skills-migration" aria-label="迁移预览"><h3>只影响{roleName}</h3>
        <p>关键词加载改为按任务选择，常驻指导保留；原有角色手动技能保持手动，内置方法逐项按以下预览调整。回退保留文件及单项设置。</p>
        <ul>{catalog.migration.map(item => <li key={item.ref}><strong>{item.name}</strong>：{legacyPolicies[item.from] ?? item.from} → {policies[item.to]}</li>)}</ul>
        <button type="button" className="primary" disabled={busy} onClick={() => configure({ enabled: true })}>为{roleName}启用自主选择</button>
      </div>}
      <div className="skills-layout"><div className="skills-list">
        <label className="skill-import">导入 Markdown<input type="file" accept=".md,text/markdown,text/plain" disabled={busy} onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void work(() => importMarkdown(file)); }} /></label>
        <p className="skills-hint">保存默认只属于当前角色。上传附件本身不会安装技能。</p>
        {catalog.skills.map(skill => <article key={skill.ref} className={`skill-card${selected?.ref === skill.ref ? ' is-selected' : ''}`}>
          <button className="skill-open" type="button" disabled={busy} onClick={() => open(skill)}><strong>{skill.name}</strong><span className="skill-description">{skill.description}</span></button>
          <div className="skill-tags"><span>{sources[skill.source] ?? skill.source}</span><span>{statuses[skill.status]}</span><span>{policies[skill.policy]}</span></div>
          <small>调用：${skill.id}{skill.effective ? ' · 当前有效版本' : ''}</small>
          {skill.problems.length > 0 && <p className="skills-error">{skill.problems.join('；')}</p>}
          {skill.ignoredMetadata.length > 0 && <p className="skills-hint">不会生效的外部设置：{skill.ignoredMetadata.join('、')}</p>}
          <div className="skill-controls"><label><input type="checkbox" checked={skill.enabled} disabled={busy} onChange={event => configure({ ref: skill.ref, active: event.target.checked })} />启用 {skill.name}</label>
            <select aria-label={`${skill.name}的使用方式`} value={skill.policy} disabled={busy} onChange={event => configure({ ref: skill.ref, policy: event.target.value as SkillPolicy })}>
              {Object.entries(policies).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></div>
        </article>)}
        {catalog.diagnostics.map((item, index) => <p className="skills-error" key={index}>{item.skillId ?? sources[item.source ?? ''] ?? '技能'}：{item.error}</p>)}
      </div>
      <div className="skills-editor" ref={editor} role="region" aria-label="技能编辑区" tabIndex={-1}>{content === null ? <div className="skills-empty"><h3>选择一个方法查看</h3><p>在这里管理角色私有技能，或查看公共与内置技能的来源、资源和使用状态。</p></div> : <>
        <h3>{selected ? selected.name : '导入或创建角色技能'}</h3>
        <p className="skills-hint">{selected && !selected.editable ? '此来源只读；可另存为角色私有副本。' : `仅保存到${roleName}的技能库。自动选择还受当前角色开关和真实工具权限约束。`}</p>
        <label className="skill-content-label">技能内容<textarea aria-label="技能内容" spellCheck={false} value={content} readOnly={Boolean(selected && !selected.editable)} disabled={busy} onChange={event => setContent(event.target.value)} /></label>
        {selected?.supportingFiles.length ? <p className="skills-hint">已有资源（保存正文会保留）：{selected.supportingFiles.map(item => item.path).join('、')}</p> : null}
        {(!selected || selected.editable) && <><label className="skill-import">添加支持文件<input type="file" multiple disabled={busy} onChange={event => { const files = Array.from(event.target.files ?? []); event.target.value = ''; void work(() => addResources(files)); }} /></label>
          {resources.map((resource, index) => <label className="skill-resource" key={index}>资源相对路径<input aria-label={`资源 ${index + 1} 路径`} value={resource.path} disabled={busy} onChange={event => setResources(previous => previous.map((item, i) => i === index ? { ...item, path: event.target.value } : item))} /></label>)}
          <div className="skills-actions"><button type="button" className="primary" disabled={busy} onClick={() => void work(async () => {
            const saved = await skillApi.save(roleId, content, document, resources);
            const updated = await refresh();
            if (!isCurrent()) return;
            setDocument(saved); setSelected(updated.skills.find(item => item.ref === saved.ref) ?? null); setContent(saved.content); setResources([]);
            setNotice(saved.problems.length ? `已保存并读回；暂不可用：${saved.problems.join('；')}` : '已保存并读回核验，下次对话可按当前使用方式复用。');
          })}>{busy ? '处理中…' : '保存并核验'}</button>
          {selected?.editable && <><button type="button" className="ghost" disabled={busy || unsaved} onClick={() => setConfirm('publish')}>发布到公共库</button><button type="button" className="ghost danger" disabled={busy || unsaved} onClick={() => setConfirm('remove')}>移除私有技能</button></>}
          </div></>}
        {selected?.editable && unsaved && <p className="skills-hint">请先保存修改，再发布或移除这个技能。</p>}
        {selected && !selected.editable && <button type="button" className="primary" disabled={busy} onClick={() => { setSelected(null); setDocument(undefined); }}>另存角色私有副本</button>}
        {confirm && selected && <div className="skills-confirm" role="group" aria-label={confirm === 'publish' ? '确认公共发布' : '确认移除'}>
          <p>{confirm === 'publish' ? '公共发布会让所有角色看到完整正文和支持资源，请确认其中没有角色私有资料。' : '移除这个私有技能及其支持资源后，同 ID 的公共或内置版本可能恢复可用。'}</p>
          <button type="button" className="primary" disabled={busy} onClick={() => void work(async () => {
            await skillApi[confirm](roleId, selected.ref, document!.contentHash);
            await refresh();
            if (isCurrent()) { setConfirm(null); setContent(null); setSelected(null); setDocument(undefined); setNotice(confirm === 'publish' ? '已发布到公共库，其他角色现在可以查看。' : '私有技能已移除。'); }
          })}>{confirm === 'publish' ? '确认发布全部内容' : '确认移除这个技能'}</button>
          <button type="button" className="ghost" disabled={busy} onClick={() => setConfirm(null)}>取消</button>
        </div>}
      </>}</div></div>
    </>}
  </section>;
}
