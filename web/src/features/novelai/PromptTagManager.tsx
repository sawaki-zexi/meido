import { useRef } from 'react';
import { Dialog } from '../../ui/Dialog';
import { Icon } from '../../ui/Icon';
import './promptTags.css';
import type { PromptTagRating } from '../../api/novelai';
import { Notice, Placeholder } from '../../ui/Status';
import { usePromptTags } from './usePromptTags';

const attachmentUrl = (roleId: string, attachmentId: string) =>
  `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachmentId)}`;
const ratingLabels: Record<PromptTagRating, string> = { general: '通用', mature: '成人向', explicit: '露骨' };

export function PromptTagManager({ roleId, expanded = false }: { roleId: string; expanded?: boolean }) {
  const tags = usePromptTags(roleId, expanded);
  const referenceInput = useRef<HTMLInputElement>(null);
  return <>
    {!expanded && <button type="button" aria-expanded={tags.open} onClick={tags.toggle}>{tags.open ? '收起提示标签' : '管理提示标签'}</button>}
    {tags.open && <section className="prompt-tag-workspace" aria-label="本角色提示标签库">
      {!tags.editorOpen && <header className="prompt-tag-header"><div><h2>提示标签库</h2><p>用关键词唤起喜欢的场景、服装与构图。</p></div><div className="prompt-tag-toolbar">
        {(tags.editing || tags.dirty) && <button type="button" disabled={tags.busy} onClick={tags.resume}>继续编辑</button>}
        <button type="button" className="primary" disabled={tags.busy || !tags.page} onClick={tags.create}><Icon name="plus" size={16} />新建标签</button>
      </div></header>}
      {tags.error && !tags.pendingDelete && <Notice onDismiss={tags.dismissError}>{tags.error}</Notice>}
      {!tags.page && !tags.error && <Placeholder loading>正在加载提示标签…</Placeholder>}
      {!tags.editorOpen && <>
      <div className="prompt-tag-filters">
      <label className="field prompt-tag-search"><span>搜索提示标签</span><input type="search" aria-label="搜索提示标签" placeholder="搜索名称、关键词或提示词" value={tags.filters.query} onChange={(event) => tags.filter({ query: event.target.value })} /></label>
      <div className="novelai-settings-grid">
        <label className="field"><span>分类</span><select aria-label="筛选标签分类" value={tags.filters.category} onChange={(event) => tags.filter({ category: event.target.value })}>
          <option value="">全部分类</option>{tags.categories.map((category) => <option key={category} value={category}>{category}</option>)}
        </select></label>
        <label className="field"><span>内容分级</span><select aria-label="筛选内容分级" value={tags.filters.rating} onChange={(event) => tags.filter({ rating: event.target.value as '' | PromptTagRating })}>
          <option value="">全部分级</option><option value="general">通用（general）</option><option value="mature">成人向（mature）</option><option value="explicit">露骨（explicit）</option>
        </select></label>
        <label className="field"><span>启用状态</span><select aria-label="筛选启用状态" value={tags.filters.status} onChange={(event) => tags.filter({ status: event.target.value as '' | 'enabled' | 'disabled' })}>
          <option value="">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option>
        </select></label>
      </div>
      <button type="button" onClick={tags.clearFilters}>清空筛选</button>
      </div>
      {tags.page && <p role="status">显示 {tags.filteredTags.length} / {tags.page.tags.length} 个标签</p>}
      {tags.page?.tags.length === 0 && <Placeholder>暂无提示标签。</Placeholder>}
      {!!tags.page?.tags.length && tags.filteredTags.length === 0 && <Placeholder>没有符合条件的标签。</Placeholder>}
      <div className="prompt-tag-grid">{tags.filteredTags.map((tag) => <article className="prompt-tag-card" key={tag.id} aria-label={`提示标签 ${tag.name}`}>
        <button type="button" className="prompt-tag-card-open" disabled={tags.busy} aria-label={`编辑标签 ${tag.name}`} onClick={() => tags.edit(tag)}>
          <div className="prompt-tag-card-image">{tag.referenceAttachment ? <img src={attachmentUrl(roleId, tag.referenceAttachment.id)} alt={`参考图 ${tag.name}`} /> : <Icon name="image" size={28} />}
            <span className={tag.enabled ? 'tag-state enabled' : 'tag-state'}>{tag.enabled ? '已启用' : '已停用'}</span>
          </div>
          <div className="prompt-tag-card-body"><h3>{tag.name}</h3><p>{tag.category} · {ratingLabels[tag.rating]}</p><div className="prompt-tag-keywords">{tag.keywords.slice(0, 4).map((keyword, index) => <span key={index}>{keyword}</span>)}{tag.keywords.length > 4 && <span>+{tag.keywords.length - 4}</span>}</div></div>
        </button>
        <button type="button" className="prompt-tag-delete" disabled={tags.busy} aria-label={`删除标签 ${tag.name}`} onClick={() => tags.requestDelete(tag)}><Icon name="trash" size={16} /></button>
      </article>)}</div>
      <div className="prompt-tag-list-footer"><span>最多 250 个标签 · 实际匹配遵循角色的标签许可</span><button type="button" disabled={tags.busy} onClick={() => void tags.reload()}>刷新标签库</button></div>
      </>}
      {tags.editorOpen && <form className="prompt-tag-editor" aria-label={tags.editing ? '编辑提示标签' : '创建提示标签'} onSubmit={(event) => { event.preventDefault(); void tags.save(); }}>
        <header className="prompt-tag-editor-header"><button type="button" disabled={tags.busy} onClick={tags.back}><Icon name="back" size={16} />返回标签库</button><h2>{tags.editing ? '编辑提示标签' : '新建提示标签'}</h2><div className="prompt-tag-toolbar"><button type="button" disabled={tags.busy || !tags.dirty} onClick={tags.resetDraft}>恢复已保存内容</button><button type="submit" className="primary" disabled={tags.busy || !tags.dirty}>{tags.busy ? '保存中…' : '保存标签'}</button></div></header>
        {tags.saved && <p className="prompt-tag-saved" role="status">标签已保存</p>}
        <fieldset disabled={tags.busy}>
          <div className="prompt-tag-editor-main">
          <label className="field"><span>标签名称</span><input aria-label="标签名称" required maxLength={80} value={tags.draft.name} onChange={(event) => tags.patch({ name: event.target.value })} /></label>
          <label className="field"><span>标签分类</span><input aria-label="标签分类" required maxLength={80} value={tags.draft.category} onChange={(event) => tags.patch({ category: event.target.value })} /></label>
          <label className="field"><span>匹配关键词（每行一个）</span><textarea aria-label="匹配关键词（每行一个）" required value={tags.keywords} onChange={(event) => tags.setKeywords(event.target.value)} /><small>1–16 个关键词，每个最多 80 字符；按文字匹配，不使用正则。</small></label>
          <label className="field"><span>正向提示词</span><textarea aria-label="标签正向 tags" maxLength={4000} value={tags.draft.prompt} onChange={(event) => tags.patch({ prompt: event.target.value })} /></label>
          <label className="field"><span>负向提示词</span><textarea aria-label="标签负向 tags" maxLength={4000} value={tags.draft.negative_prompt} onChange={(event) => tags.patch({ negative_prompt: event.target.value })} /></label>
          <label className="field"><span>内容分级</span><select aria-label="标签 rating" value={tags.draft.rating} onChange={(event) => tags.patch({ rating: event.target.value as PromptTagRating })}>
            {Object.entries(ratingLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <label><input type="checkbox" checked={tags.draft.enabled} onChange={(event) => tags.patch({ enabled: event.target.checked })} />启用此标签</label>
          </div><aside className="prompt-tag-editor-reference"><h3>参考图</h3><p>帮助你辨认标签，不作为生图参考输入。</p>
          <input ref={referenceInput} className="prompt-tag-file" aria-label="上传参考图" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { const file = event.target.files?.[0]; event.currentTarget.value = ''; if (file) void tags.uploadReference(file); }} />
          {tags.draft.reference_attachment_id ? <div className="prompt-tag-reference-preview">
            <img src={attachmentUrl(roleId, tags.draft.reference_attachment_id)} alt="当前参考图" />
            <div><button type="button" onClick={() => referenceInput.current?.click()}>更换参考图</button><button type="button" onClick={() => tags.patch({ reference_attachment_id: null })}>移除参考图</button></div>
          </div> : <button type="button" className="prompt-tag-add-image" onClick={() => referenceInput.current?.click()}><Icon name="image" size={28} /><span>添加参考图</span><small>PNG / JPEG / WebP</small></button>}
          </aside>
        </fieldset>
      </form>}
      {!tags.editorOpen && <details className="prompt-tag-preview"><summary>本地匹配预览</summary>
      <form aria-label="本地标签预览" onSubmit={(event) => { event.preventDefault(); void tags.inspect(); }}>
        <label className="field"><span>预览当前主人输入</span><textarea aria-label="预览当前主人输入" required maxLength={12000} value={tags.text} onChange={(event) => tags.setText(event.target.value)} /></label>
        <label className="field"><span>预览基础正向 tags</span><textarea aria-label="预览基础正向 tags" maxLength={4000} value={tags.prompt} onChange={(event) => tags.setPrompt(event.target.value)} /></label>
        <label className="field"><span>预览基础负向 tags</span><textarea aria-label="预览基础负向 tags" maxLength={4000} value={tags.negativePrompt} onChange={(event) => tags.setNegativePrompt(event.target.value)} /></label>
        <button type="submit" disabled={tags.busy}>预览标签扩写</button>
      </form>
      {tags.preview && <section aria-label="标签扩写结果"><p>匹配版本 {tags.preview.revision}</p>
        <p>命中标签：{tags.preview.matchedIds.length ? tags.preview.matchedIds.map((id) => tags.page?.tags.find((tag) => tag.id === id)?.name ?? id).join('、') : '无'}</p>
        <p>最终正向 tags：</p><p>{tags.preview.prompt || '（空）'}</p><p>最终负向 tags：</p><p>{tags.preview.negative_prompt || '（空）'}</p>
      </section>}
      </details>}
      {tags.pendingDelete && <Dialog title="删除提示标签" variant="compact" modalFocus onClose={tags.cancelDelete}>
        <p>确定删除「{tags.pendingDelete.name}」？删除后将不再参与提示词匹配。</p>
        {tags.error && <Notice onDismiss={tags.dismissError}>{tags.error}</Notice>}
        <div className="form-actions"><button type="button" disabled={tags.busy} onClick={tags.cancelDelete}>取消</button><button type="button" className="danger" disabled={tags.busy} onClick={() => void tags.remove(tags.pendingDelete!)}>{tags.busy ? '正在删除…' : '确认删除'}</button></div>
      </Dialog>}
    </section>}
  </>;
}
