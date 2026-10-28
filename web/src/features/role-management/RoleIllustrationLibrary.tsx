import { useEffect, useRef, useState } from 'react';
import type { Role, RoleIllustration } from '../../api/types';
import { roleAssets } from '../../api/roleAssets';
import { errorMessage } from '../../api/client';
import { Dialog } from '../../ui/Dialog';
import './roleIllustrations.css';
import { RoleMoodBindingEditor } from './RoleMoodBindingEditor';

export function RoleIllustrationLibrary(props: { role: Role; onRoleSaved: (role: Role) => void }) {
  return <Illustrations key={props.role.id} {...props} />;
}
function Illustrations({ role, onRoleSaved }: { role: Role; onRoleSaved: (role: Role) => void }) {
  const [current, setCurrent] = useState(role);
  const [filter, setFilter] = useState('');
  const [query, setQuery] = useState('');
  const [categoryName, setCategoryName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<RoleIllustration | null>(null);
  const [removing, setRemoving] = useState(false);
  const [categoryRemoval, setCategoryRemoval] = useState<{ name: string; images: RoleIllustration[] } | null>(null);
  const [message, setMessage] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const pending = useRef(false);
  useEffect(() => { setCurrent(role); }, [role]);
  useEffect(() => { alive.current = true; void roleAssets.list(role.id).then(({ role: fresh }) => {
    if (alive.current) { setCurrent(fresh); onRoleSaved(fresh); }
  }).catch(cause => { if (alive.current) setError(errorMessage(cause, '无法读取角色插画')); });
  return () => { alive.current = false; }; }, [role.id]);
  const accept = (saved: Role) => {
    if (!alive.current) return;
    setCurrent(saved); onRoleSaved(saved);
    setSelected(item => saved.illustrations?.find(value => item?.id === value.id) ?? null);
  };
  const run = async (action: () => Promise<{ role: Role }>, success: string) => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(''); setMessage('');
    try { const { role: saved } = await action(); if (!alive.current) return;
      accept(saved); setMessage(success); setRemoving(false); return true;
    } catch (cause) { if (alive.current) setError(errorMessage(cause, '操作未确认，请刷新后检查')); }
    finally { pending.current = false; if (alive.current) setBusy(false); }
  };
  const categories = current.illustrationCategories ?? ['未分类'];
  const images = current.illustrations ?? [];
  const shown = images.filter(item => (!filter || item.category === filter) && item.name.normalize('NFKC').toLowerCase().includes(query.normalize('NFKC').toLowerCase()));
  const renameCategory = async () => {
    const name = categoryName.trim(); if (!filter || !name) return;
    const saved = await run(() => roleAssets.update(role.id, { categories: categories.map(item => item === filter ? name : item),
      categorySend: { [name]: Boolean(current.illustrationCategorySend?.[filter]) },
      bindings: Object.fromEntries(images.filter(item => item.category === filter).map(item => [item.id, name])) }), '插画分类已改名。');
    if (saved) { setFilter(name); setCategoryName(''); }
  };
  const uploadFiles = (files: File[]) => {
    const category = filter || categories[0];
    void run(async () => {
      let saved = current;
      for (let index = 0; index < files.length && alive.current; index++) {
        try { ({ role: saved } = await roleAssets.upload(role.id, files[index], category)); accept(saved); }
        catch (cause) { throw new Error(`已保存 ${index}/${files.length} 张；「${files[index].name}」上传未确认，已停止后续上传。请刷新核对后再处理剩余文件。${errorMessage(cause, '')}`); }
      }
      return { role: saved };
    }, `${files.length} 张插画已保存。`);
  };
  const removeCategoryImages = async () => {
    if (!categoryRemoval) return;
    const target = categoryRemoval;
    // Close confirmation before dispatch: an uncertain result must be checked,
    // not retried by clicking the same confirmation again.
    setCategoryRemoval(null);
    const completed = await run(async () => {
      let saved = current;
      for (let index = 0; index < target.images.length && alive.current; index++) {
        try { ({ role: saved } = await roleAssets.remove(role.id, target.images[index].id)); accept(saved); }
        catch (cause) { throw new Error(`已移出 ${index}/${target.images.length} 张；「${target.images[index].name}」移出未确认，分类暂未移除。请刷新核对，不会自动重复提交。${errorMessage(cause, '')}`); }
      }
      if (!alive.current) return { role: saved };
      const remaining = (saved.illustrationCategories ?? categories).filter(name => name !== target.name);
      return roleAssets.update(role.id, { categories: remaining.length ? remaining : ['未分类'],
        ...(remaining.length ? {} : { categorySend: { 未分类: false } }) });
    }, '分类及其中插画已移出；原文件保留在本地恢复区，仅支持手动恢复。');
    if (completed) setFilter('');
  };
  return <section className="role-illustrations" aria-label={`${role.name}的角色插画`}>
    <header><div><h2>角色插画与聊天背景</h2><p>原尺寸保存插画；聊天背景独立于头像、卡片图和表情素材。</p></div>
      <button type="button" disabled={busy} onClick={() => input.current?.click()}>上传插画</button>
      <input ref={input} hidden type="file" multiple aria-label="上传角色插画" accept="image/png,image/jpeg,image/webp" onChange={event => {
        const files = Array.from(event.currentTarget.files ?? []); event.currentTarget.value = '';
        if (files.length) uploadFiles(files);
      }} />
    </header>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <div className="illustration-toolbar"><label>搜索插画<input aria-label="搜索插画" value={query} onChange={event => setQuery(event.target.value)} /></label>
      <label>插画分类<select aria-label="插画分类" value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部分类</option>{categories.map(name => <option key={name}>{name}</option>)}</select></label>
      <button type="button" disabled={busy || !current.chatBackgroundId} onClick={() => void run(() => roleAssets.background(role.id, null), '已恢复默认聊天背景，插画仍保留。')}>恢复默认聊天背景</button>
      <button type="button" disabled={busy} onClick={() => void run(() => roleAssets.list(role.id), '插画库已刷新。')}>刷新插画库</button>
    </div>
    {filter && <label className="illustration-send-permission"><input type="checkbox" checked={Boolean(current.illustrationCategorySend?.[filter])} disabled={busy} onChange={event => {
      const enabled = event.target.checked;
      void run(() => roleAssets.update(role.id, { categorySend: { [filter]: enabled } }), enabled ? '角色可以在获准会话中发送此分类插画。' : '已禁止角色发送此分类插画。');
    }} />允许角色发送此分类插画（仍遵循会话及渠道权限）</label>}
    {Object.keys(current.moodIllustrationBindings ?? {}).length > 0 && <label className="field">默认心情<select aria-label="默认心情" value={current.defaultMood} disabled={busy} onChange={event => {
      const mood = event.target.value;
      void run(() => roleAssets.update(role.id, { defaultMood: mood }), '默认心情已保存。');
    }}>{Object.keys(current.moodIllustrationBindings ?? {}).map(mood => <option key={mood}>{mood}</option>)}</select></label>}
    <details><summary>管理插画分类</summary><div className="illustration-toolbar"><label>分类名称<input maxLength={80} aria-label="插画分类名称" value={categoryName} onChange={event => setCategoryName(event.target.value)} /></label>
      <button type="button" disabled={busy || !categoryName.trim()} onClick={() => { void run(() => roleAssets.update(role.id, { categories: [...categories, categoryName.trim()] }), '插画分类已添加。'); setCategoryName(''); }}>添加分类</button>
      <button type="button" disabled={busy || !filter || !categoryName.trim()} onClick={renameCategory}>重命名所选分类</button>
      <button type="button" disabled={busy || !filter || categories.length < 2} onClick={() => {
        const remaining = categories.filter(name => name !== filter);
        void run(() => roleAssets.update(role.id, { categories: remaining, bindings: Object.fromEntries(images.filter(item => item.category === filter).map(item => [item.id, remaining[0]])) }), '分类已移除，其中插画已移入其他分类。'); setFilter('');
      }}>移除所选分类并保留插画</button>
      <button type="button" className="danger" disabled={busy || !filter} onClick={() => setCategoryRemoval({ name: filter, images: images.filter(item => item.category === filter) })}>移除所选分类及其中插画</button>
    </div></details>
    {shown.length === 0 && <p>{images.length ? '没有符合条件的插画。' : '暂无角色插画。'}</p>}
    <div className="illustration-grid">{shown.map(item => <button type="button" key={item.id} className="illustration-card" aria-label={`查看插画 ${item.name}`} onClick={() => { setSelected(item); setRemoving(false); }}>
      <img src={item.url} alt={item.name} loading="lazy" /><span>{item.name}</span><small>{item.category}{current.chatBackgroundId === item.id ? ' · 当前聊天背景' : ''}{Object.keys(current.moodIllustrationBindings ?? {}).find(mood => current.moodIllustrationBindings?.[mood] === item.id) ? ` · 心情：${Object.keys(current.moodIllustrationBindings ?? {}).find(mood => current.moodIllustrationBindings?.[mood] === item.id)}` : ''}</small>
    </button>)}</div>
    {categoryRemoval && <Dialog title="移除分类及插画" modalFocus onClose={() => { if (!busy) setCategoryRemoval(null); }}>
      <p>确认移除「{categoryRemoval.name}」分类及其中 {categoryRemoval.images.length} 张插画？背景和心情绑定会同时清理；原文件保留在本地恢复区，仅支持手动恢复。最后一个分类移除后会保留空的「未分类」。</p>
      <div className="form-actions"><button type="button" disabled={busy} onClick={() => setCategoryRemoval(null)}>取消</button>
        <button type="button" className="danger" disabled={busy} onClick={() => void removeCategoryImages()}>确认移除分类及插画</button></div>
    </Dialog>}
    {selected && <Dialog title={selected.name} modalFocus onClose={() => { if (!busy) { setSelected(null); setRemoving(false); } }}>
      <img className="illustration-preview" src={selected.url} alt={`${selected.name} 原图`} />
      {error && <p role="alert">{error}</p>}
      <label className="field">所属分类<select value={selected.category} disabled={busy} onChange={event => void run(() => roleAssets.update(role.id, { bindings: { [selected.id]: event.target.value } }), '插画已移动。')}>{categories.map(name => <option key={name}>{name}</option>)}</select></label>
      <RoleMoodBindingEditor key={`${selected.id}:${JSON.stringify(current.moodIllustrationBindings)}`} role={current} illustrationId={selected.id} busy={busy}
        onSave={bindings => void run(() => roleAssets.update(role.id, { moodBindings: bindings }), '心情插画绑定已保存。')} />
      <div className="form-actions"><a href={selected.url} download={selected.name}>下载原图</a><button type="button" disabled={busy} onClick={() => void run(() => roleAssets.background(role.id, selected.id), '已设为聊天背景。')}>设为聊天背景</button>
        <button type="button" disabled={busy} onClick={() => void run(() => roleAssets.avatar(role.id, selected.id), '已设为角色头像。')}>设为角色头像</button><button type="button" className="danger" disabled={busy} onClick={() => setRemoving(true)}>移出插画库</button></div>
      {removing && <div role="alert"><p>确认移出这张插画？若为聊天背景将恢复默认；原文件保留在本地恢复区。</p><button type="button" disabled={busy} onClick={() => setRemoving(false)}>取消</button><button type="button" className="danger" disabled={busy} onClick={() => void run(() => roleAssets.remove(role.id, selected.id), '插画已移出，原文件保留在本地恢复区。')}>确认移出插画</button></div>}
    </Dialog>}
  </section>;
}
