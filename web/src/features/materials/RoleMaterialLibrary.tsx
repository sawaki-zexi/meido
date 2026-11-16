import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, errorMessage } from '../../api/client';
import { materials, type MaterialLibrary, type RoleMaterial, type MaterialChanges } from '../../api/materials';
import { Dialog } from '../../ui/Dialog';
import { Icon } from '../../ui/Icon';
import './materials.css';
import { MaterialMeaningEditor } from './MaterialMeaningEditor';
import { collectionFeedback } from './collectionFeedback';

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const sourceLabels: Record<RoleMaterial['source'], string> = { owner_import: '主人导入', chat: '聊天保存', drift: 'Drift 整理', legacy: '已有素材' };
const basisLabels = { owner_confirmed: '主人确认', model_observation: '模型观察', legacy_description: '已有说明', unverified: '待补充说明' };
const supportedImage = (file: File) => ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)
  || (!file.type && /\.(png|jpe?g|webp|gif)$/i.test(file.name));
const requiresRead = (cause: unknown) => !(cause instanceof ApiError) || cause.status === 409 || cause.status >= 500;
type ObservedMaterial = RoleMaterial & { observedRevision: string };
type LibraryObservation = Omit<MaterialLibrary, 'materials'> & { materials: ObservedMaterial[] };
const observe = (material: RoleMaterial, revision: string): ObservedMaterial => ({ ...material, observedRevision: revision });

/** A role switch disposes all previous reads, drafts, dialogs and mutation feedback. */
export function RoleMaterialLibrary(props: { roleId: string; roleName: string }) {
  return <MaterialLibraryContent key={props.roleId} {...props} />;
}

function MaterialLibraryContent({ roleId, roleName }: { roleId: string; roleName: string }) {
  const [library, setLibrary] = useState<LibraryObservation | null>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState('');
  const [source, setSource] = useState('');
  const [enabled, setEnabled] = useState('');
  const [offset, setOffset] = useState(0);
  const filtersRef = useRef({ query, category, offset, source, enabled });
  filtersRef.current = { query, category, offset, source, enabled };
  const [preview, setPreview] = useState<ObservedMaterial | null>(null);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [importCategory, setImportCategory] = useState('');
  const [description, setDescription] = useState('');
  const [fileError, setFileError] = useState('');
  const [actionError, setActionError] = useState('');
  const [needsRead, setNeedsRead] = useState(false);
  const [busy, setBusy] = useState<'import' | 'remove' | 'edit' | null>(null);
  const [focusAfterMutation, setFocusAfterMutation] = useState(false);
  const importOpenRef = useRef(importOpen);
  importOpenRef.current = importOpen;
  const previewRef = useRef(preview);
  previewRef.current = preview;
  const busyRef = useRef(false);
  const alive = useRef(false);
  const readController = useRef<AbortController | null>(null);
  const readVersion = useRef(0);
  const importButton = useRef<HTMLButtonElement>(null);
  const libraryRoot = useRef<HTMLElement>(null);
  const gallery = useRef<HTMLDivElement>(null);

  const load = useCallback(async (removedId?: string) => {
    const version = ++readVersion.current;
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    setLoading(true); setReadError('');
    try {
      const result = await materials.list(roleId, controller.signal, filtersRef.current);
      const observation: LibraryObservation = { ...result, materials: result.materials.map(item => observe(item, result.revision)) };
      const activePreview = previewRef.current?.id === removedId ? null : previewRef.current;
      let refreshedPreview = activePreview ? observation.materials.find(item => item.id === activePreview.id) : null;
      if (activePreview && !refreshedPreview) {
        try {
          const detail = await materials.get(roleId, activePreview.id, controller.signal);
          // Detail and list can observe different revisions. A detail token
          // authorizes only the material returned with it, never older cards.
          refreshedPreview = observe(detail.material, detail.revision);
        } catch (cause) { if (!(cause instanceof ApiError && cause.status === 404)) throw cause; }
      }
      if (!alive.current || version !== readVersion.current) return;
      setLibrary(observation); setNeedsRead(false); setActionError(''); setRemoveOpen(false);
      setPreview(current => current?.id === activePreview?.id ? refreshedPreview ?? null : current);
      return true;
    } catch (cause) {
      if (alive.current && version === readVersion.current) setReadError(errorMessage(cause, '无法读取素材库'));
      return false;
    } finally {
      if (alive.current && version === readVersion.current) setLoading(false);
    }
  }, [roleId]);

  useEffect(() => {
    alive.current = true; void load();
    return () => { alive.current = false; readVersion.current++; readController.current?.abort(); };
  }, [load, query, category, offset, source, enabled]);

  useEffect(() => {
    if (focusAfterMutation && !busy && !loading && !importOpen && !preview) {
      const active = document.activeElement;
      // A slow refresh must not undo a newer keyboard/pointer interaction.
      if (!active || active === document.body || active === libraryRoot.current || !active.isConnected) importButton.current?.focus();
      setFocusAfterMutation(false);
    }
  }, [focusAfterMutation, busy, loading, importOpen, preview]);

  const onImport = async (event: FormEvent) => {
    event.preventDefault();
    if (!file || !library || busyRef.current || loading || needsRead || fileError) return;
    busyRef.current = true; setBusy('import'); setActionError(''); setFeedback('');
    try {
      const result = await materials.import(roleId, file, importCategory.trim() || '导入', description.trim(), library.revision);
      if (!alive.current) return;
      setFocusAfterMutation(importOpenRef.current);
      setImportOpen(false); setFile(null); setDescription(''); setImportCategory('');
      setLibrary(current => current ? { ...current, materials: [...current.materials.filter(item => item.id !== result.material.id), observe(result.material, result.revision)] } : current);
      setFeedback(collectionFeedback(result, result.deduplicated ? '这张图片已在素材库中，没有重复导入。' : '素材已导入当前角色的素材库。'));
      await load();
    } catch (cause) {
      if (alive.current) { setActionError(errorMessage(cause, '无法导入素材')); setNeedsRead(requiresRead(cause)); }
    } finally {
      busyRef.current = false;
      if (alive.current) setBusy(null);
    }
  };

  const onRemove = async () => {
    if (!preview || !library || busyRef.current || loading || needsRead) return;
    busyRef.current = true; setBusy('remove'); setActionError(''); setFeedback('');
    try {
      await materials.remove(roleId, preview.id, preview.observedRevision);
      if (!alive.current) return;
      setFocusAfterMutation(previewRef.current?.id === preview.id);
      setRemoveOpen(false); setPreview(current => current?.id === preview.id ? null : current);
      setLibrary(current => current ? { ...current, materials: current.materials.filter(item => item.id !== preview.id) } : current);
      setFeedback('已移出素材库，原图保留在本地恢复区；聊天中的图片不受影响。');
      await load(preview.id);
    } catch (cause) {
      if (alive.current) { setActionError(errorMessage(cause, '无法移出素材')); setNeedsRead(requiresRead(cause)); }
    } finally {
      busyRef.current = false;
      if (alive.current) setBusy(null);
    }
  };

  const onEdit = async (item: ObservedMaterial, changes: MaterialChanges) => {
    if (!library || busyRef.current || loading || needsRead) return;
    busyRef.current = true; setBusy('edit'); setActionError(''); setFeedback('');
    try {
      const result = await materials.edit(roleId, item.id, item.observedRevision, changes);
      if (!alive.current) return;
      const updated = observe(result.material, result.revision);
      setLibrary(current => current ? { ...current, materials: current.materials.map(value => value.id === item.id ? updated : value) } : current);
      setPreview(current => current?.id === item.id ? updated : current);
      setFeedback('素材说明与聊天设置已保存。');
      // Fetch the other cards before enabling another edit. Their observations
      // cannot inherit this response's revision even after our own mutation.
      if (!await load() && alive.current) setNeedsRead(true);
    } catch (cause) {
      if (alive.current) { setActionError(errorMessage(cause, '无法修改素材')); setNeedsRead(requiresRead(cause)); }
    } finally { busyRef.current = false; if (alive.current) setBusy(null); }
  };

  const mutationError = <>{readError && (importOpen || removeOpen || preview) && <p className="material-error" role="alert">{readError}</p>}{actionError && <p className="material-error" role="alert">{actionError}</p>}{needsRead && <div className="material-recovery"><p>请先重新读取素材库，核对操作结果；不会自动重复提交。编辑草稿会保留，可展开已保存内容进行比较。</p><button type="button" className="ghost" disabled={loading || Boolean(busy)} onClick={() => void load()}>重新读取素材库</button></div>}</>;
  const search = query.trim().toLocaleLowerCase();
  const filtered = Boolean(search || category || source || enabled);
  const visible = library?.materials ?? [];
  const total = library?.total ?? visible.length;
  const categories = [...new Set([...(library?.categories.map(item => item.name) ?? []), ...(library?.materials.map(item => item.category) ?? [])])];

  return <section ref={libraryRoot} tabIndex={-1} className="material-library" aria-label={`${roleName}的素材库`}>
    <header className="material-library-heading"><div><h2>素材库</h2><p>当前角色独享的图片与表情，不是图片生成历史。</p></div>
      <button ref={importButton} type="button" className="primary" disabled={!library || loading || Boolean(busy) || needsRead} onClick={() => { setImportOpen(true); setFileError(''); setActionError(''); }}><Icon name="upload" size={17} />导入素材</button>
    </header>
    <div className="material-location"><Icon name="image" size={19} /><div><p>保存位置：<strong>角色工作目录 / workspace/memes</strong></p><p>只属于{roleName}；主人查看、导入和移出素材不需要开启 Drift 图片权限。</p></div></div>
    <div className="material-filters"><label><span>搜索素材</span><input type="search" aria-label="搜索素材" value={query} placeholder="名称、描述或别名" onChange={event => { setQuery(event.target.value); setOffset(0); }} /></label>
      <label><span>素材分类</span><select aria-label="素材分类" value={category} onChange={event => { setCategory(event.target.value); setOffset(0); }}><option value="">全部分类</option>{categories.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      <label><span>素材来源</span><select aria-label="素材来源" value={source} onChange={event => { setSource(event.target.value); setOffset(0); }}><option value="">全部来源</option>{Object.entries(sourceLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label><span>聊天启停</span><select aria-label="聊天启停" value={enabled} onChange={event => { setEnabled(event.target.value); setOffset(0); }}><option value="">全部状态</option><option value="true">已启用</option><option value="false">已停用</option></select></label>
      <button type="button" className="ghost" disabled={loading || Boolean(busy)} aria-label="重新读取素材库" onClick={() => void load()}><Icon name="refresh" size={17} />刷新</button>
    </div>
    {(loading || feedback) && <p className="material-feedback" role="status">{loading ? '正在读取素材库…' : feedback}</p>}
    {readError && !importOpen && !removeOpen && !preview && <p role="alert" className="material-error">{readError}</p>}
    {!importOpen && !removeOpen && !preview && mutationError}
    {library && <><p className="material-count">{filtered ? `找到 ${total} 项` : `${total} 项素材`}<span>点击图片查看原图</span></p>
      {visible.length ? <div className="material-grid" ref={gallery} tabIndex={-1} aria-label="素材列表">{visible.map(item => <article className="material-card" key={item.id}>
        <MaterialImage material={item} onPreview={() => { setPreview(item); setActionError(''); }} />
        <div className="material-card-copy"><h3>{item.name}</h3><p>{item.description || '待补充说明，不自动推荐'}</p><label className="material-chat-toggle"><input type="checkbox" aria-label={`${item.name}用于聊天`} checked={item.enabled} disabled={item.categoryEnabled === false || loading || Boolean(busy) || needsRead} onChange={event => void onEdit(item, { enabled: event.target.checked })} />用于聊天{item.categoryEnabled === false && <span>（分类已停用）</span>}</label></div>
      </article>)}</div> : <div className="material-empty"><Icon name="image" size={34} /><h3>{filtered ? '没有匹配的素材' : '还没有素材'}</h3><p>{filtered ? '试试其他关键词或分类。' : '导入 PNG、JPEG、WebP 或 GIF，保留原始图片和 GIF 动画。'}</p>{filtered && <button type="button" className="ghost" onClick={() => { setQuery(''); setCategory(''); setSource(''); setEnabled(''); setOffset(0); }}>清除筛选</button>}</div>}
      {(offset > 0 || library.nextOffset != null) && <nav className="material-dialog-actions" aria-label="素材分页">
        <button type="button" className="ghost" disabled={loading || Boolean(busy) || offset === 0} onClick={() => setOffset(Math.max(0, offset - 24))}>上一页</button>
        <span>第 {Math.floor(offset / 24) + 1} 页</span>
        <button type="button" className="ghost" disabled={loading || Boolean(busy) || library.nextOffset == null} onClick={() => setOffset(library.nextOffset ?? offset)}>下一页</button>
      </nav>}
    </>}

    {importOpen && <Dialog title="导入素材" modalFocus onClose={() => setImportOpen(false)} returnFocus={() => importButton.current?.disabled ? libraryRoot.current : importButton.current}>
      <form className="material-import" onSubmit={onImport}>
        <p>仅导入到{roleName}的素材库。支持 PNG、JPEG、WebP、GIF，每张不超过 10 MB；不会发送给模型。</p>
        <label className="field"><span>图片文件</span><input type="file" aria-label="图片文件" accept="image/png,image/jpeg,image/webp,image/gif,.png,.jpg,.jpeg,.webp,.gif" disabled={Boolean(busy)} onChange={event => {
          const next = event.target.files?.[0] ?? null;
          const issue = next && !supportedImage(next) ? '请选择 PNG、JPEG、WebP 或 GIF 图片。' : next && next.size > MAX_IMAGE_BYTES ? '图片不能超过 10 MB。' : next && !next.size ? '图片文件为空，请重新选择。' : '';
          setFileError(issue); setFile(issue ? null : next);
        }} /></label>
        {file && <p className="field-hint">{file.name} · {formatSize(file.size)}</p>}
        <label className="field"><span>分类</span><input aria-label="分类" maxLength={80} placeholder="导入" value={importCategory} disabled={Boolean(busy)} onChange={event => setImportCategory(event.target.value)} /></label>
        <label className="field"><span>描述（可选）</span><textarea aria-label="描述（可选）" rows={3} maxLength={2000} placeholder="这张图片适合什么情境？留空可收藏，暂不自动推荐。" value={description} disabled={Boolean(busy)} onChange={event => setDescription(event.target.value)} /></label>
        {fileError && <p className="material-error" role="alert">{fileError}</p>}{mutationError}
        <div className="material-dialog-actions"><button type="button" className="ghost" onClick={() => setImportOpen(false)}>取消</button><button className="primary" type="submit" disabled={!file || loading || Boolean(busy) || needsRead || Boolean(fileError)}>{busy === 'import' ? '导入中…' : '确认导入'}</button></div>
        {busy === 'import' && <p role="status">关闭此窗口不会撤销已经提交的导入。</p>}
      </form>
    </Dialog>}
    {preview && <Dialog title={preview.name} modalFocus onClose={() => { setPreview(null); setRemoveOpen(false); }} returnFocus={() => gallery.current?.querySelector<HTMLButtonElement>(`[data-material-id="${CSS.escape(preview.id)}"]`) ?? (importButton.current?.disabled ? libraryRoot.current : importButton.current)}>
      <div className="material-preview">{!removeOpen && mutationError}{busy === 'remove' && <p role="status">正在移出素材；关闭此窗口不会撤销已经提交的操作。</p>}<MaterialImage key={preview.id} material={preview} original />
        <dl><dt>名称</dt><dd>{preview.name}</dd><dt>分类</dt><dd>{preview.category}</dd><dt>描述</dt><dd>{preview.description || '暂无描述'}</dd><dt>来源</dt><dd>{sourceLabels[preview.source] ?? '已有素材'}</dd><dt>依据</dt><dd>{preview.basis ? basisLabels[preview.basis.kind] : '已有说明'}</dd>{preview.sources && <><dt>范围</dt><dd>{[...new Set(preview.sources.map(source => source.scope))].join('、')} · {preview.sources.length} 个来源</dd></>}<dt>格式</dt><dd>{preview.format}{preview.animated ? ' · 动图' : ''} · {preview.width} × {preview.height} · {formatSize(preview.size)}</dd>{preview.aliases.length > 0 && <><dt>别名</dt><dd>{preview.aliases.join('、')}</dd></>}</dl>
        <MaterialMeaningEditor key={`meaning-${preview.id}`} material={preview} disabled={Boolean(busy) || loading || needsRead} onSave={changes => onEdit(preview, changes)} />
        <button type="button" className="danger" disabled={Boolean(busy) || loading || needsRead} onClick={() => { setActionError(''); setRemoveOpen(true); }}><Icon name="trash" size={17} />移出素材库</button>
      </div>
    </Dialog>}
    {removeOpen && preview && <Dialog title="移出素材库？" variant="compact" modalFocus onClose={() => setRemoveOpen(false)}>
      <div className="material-remove"><p>将“{preview.name}”移出{roleName}的素材库？</p><p>原图保留在本地恢复区，聊天中的图片不受影响。这里暂不提供一键恢复。</p>{mutationError}<div className="material-dialog-actions"><button type="button" className="ghost" onClick={() => setRemoveOpen(false)}>取消</button><button type="button" className="danger" disabled={Boolean(busy) || loading || needsRead} onClick={() => void onRemove()}>{busy === 'remove' ? '正在移出…' : '确认移出'}</button></div></div>
    </Dialog>}
  </section>;
}

function MaterialImage({ material, original = false, onPreview }: { material: RoleMaterial; original?: boolean; onPreview?: () => void }) {
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [attempt, setAttempt] = useState(0);
  const picture = <><img key={attempt} src={material.imageUrl} alt={`${material.name}${original ? ' 原图' : ''}`} loading={original ? 'eager' : 'lazy'} onLoad={() => setStatus('ready')} onError={() => setStatus('error')} />
    {status === 'loading' && <span className="material-image-loading" aria-hidden="true"><Icon name="image" size={24} /></span>}
    {material.format === 'GIF' && <span className="material-format">GIF{material.animated ? ' · 动图' : ''}</span>}</>;
  return <div className={`material-image${original ? ' is-original' : ''}${status === 'error' ? ' has-error' : ''}`} aria-busy={status === 'loading'}>
    {status === 'error' ? <div className="material-image-error"><Icon name="image" size={28} /><p>图片暂时无法显示</p><button type="button" className="ghost" aria-label={`重试加载${material.name}`} onClick={() => { setStatus('loading'); setAttempt(value => value + 1); }}>重试加载</button>{onPreview && <button type="button" className="ghost" aria-label={`预览${material.name}`} data-material-id={material.id} onClick={onPreview}>查看详情</button>}</div>
      : onPreview ? <button type="button" className="material-preview-button" aria-label={`预览${material.name}`} data-material-id={material.id} onClick={onPreview}>{picture}</button> : picture}
  </div>;
}
function formatSize(size: number) { return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`; }
