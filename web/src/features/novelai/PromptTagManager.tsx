import type { PromptTagRating } from '../../api/novelai';
import { Notice, Placeholder } from '../../ui/Status';
import { usePromptTags } from './usePromptTags';

const attachmentUrl = (roleId: string, attachmentId: string) =>
  `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachmentId)}`;

export function PromptTagManager({ roleId, expanded = false }: { roleId: string; expanded?: boolean }) {
  const tags = usePromptTags(roleId, expanded);
  return <>
    {!expanded && <button type="button" aria-expanded={tags.open} onClick={tags.toggle}>{tags.open ? '收起提示标签' : '管理提示标签'}</button>}
    {tags.open && <section aria-label="本角色提示标签库">
      <h3>本角色提示标签库</h3>
      <p>最多 250 个标签。管理与预览只在本地进行；实际匹配取决于角色已保存的标签许可、启用状态和内容分级许可。</p>
      {tags.error && <Notice onDismiss={tags.dismissError}>{tags.error}</Notice>}
      {!tags.page && !tags.error && <Placeholder loading>正在加载提示标签…</Placeholder>}
      <label className="field"><span>搜索提示标签</span><input type="search" aria-label="搜索提示标签" placeholder="搜索名称、关键词或提示词" value={tags.filters.query} onChange={(event) => tags.filter({ query: event.target.value })} /></label>
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
      {tags.page && <p role="status">显示 {tags.filteredTags.length} / {tags.page.tags.length} 个标签</p>}
      {tags.page?.tags.length === 0 && <Placeholder>暂无提示标签。</Placeholder>}
      {!!tags.page?.tags.length && tags.filteredTags.length === 0 && <Placeholder>没有符合条件的标签。</Placeholder>}
      {tags.filteredTags.map((tag) => <article key={tag.id} aria-label={`提示标签 ${tag.name}`}>
        <h4>{tag.name}</h4><p>{tag.enabled ? '已启用' : '已停用'} · {tag.category} · {tag.rating} · 关键词：{tag.keywords.join('、')}</p>
        {tag.referenceAttachment && <div className="attachment-preview"><img src={attachmentUrl(roleId, tag.referenceAttachment.id)} alt={`参考图 ${tag.name}`} /></div>}
        <button type="button" disabled={tags.busy} aria-label={`编辑标签 ${tag.name}`} onClick={() => tags.edit(tag)}>编辑</button>
        <button type="button" disabled={tags.busy} aria-label={`删除标签 ${tag.name}`} onClick={() => void tags.remove(tag)}>删除</button>
      </article>)}
      <button type="button" disabled={tags.busy} onClick={() => void tags.reload()}>刷新标签库</button>
      <form aria-label={tags.editing ? '编辑提示标签' : '创建提示标签'} onSubmit={(event) => { event.preventDefault(); void tags.save(); }}>
        <fieldset disabled={tags.busy}>
          <legend>{tags.editing ? '编辑提示标签' : '创建提示标签'}</legend>
          <label className="field"><span>标签名称</span><input aria-label="标签名称" required maxLength={80} value={tags.draft.name} onChange={(event) => tags.patch({ name: event.target.value })} /></label>
          <label className="field"><span>标签分类</span><input aria-label="标签分类" required maxLength={80} value={tags.draft.category} onChange={(event) => tags.patch({ category: event.target.value })} /></label>
          <label className="field"><span>匹配关键词（每行一个）</span><textarea aria-label="匹配关键词（每行一个）" required value={tags.keywords} onChange={(event) => tags.setKeywords(event.target.value)} /><small>1–16 个关键词，每个最多 80 字符；按文字匹配，不使用正则。</small></label>
          <label className="field"><span>标签正向 tags</span><textarea aria-label="标签正向 tags" maxLength={4000} value={tags.draft.prompt} onChange={(event) => tags.patch({ prompt: event.target.value })} /></label>
          <label className="field"><span>标签负向 tags</span><textarea aria-label="标签负向 tags" maxLength={4000} value={tags.draft.negative_prompt} onChange={(event) => tags.patch({ negative_prompt: event.target.value })} /></label>
          <label className="field"><span>标签 rating</span><select aria-label="标签 rating" value={tags.draft.rating} onChange={(event) => tags.patch({ rating: event.target.value as PromptTagRating })}>
            <option value="general">general</option><option value="mature">mature</option><option value="explicit">explicit</option></select></label>
          <label className="field"><span>参考图</span><input aria-label="上传参考图" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => {
            const file = event.target.files?.[0];
            event.currentTarget.value = '';
            if (file) void tags.uploadReference(file);
          }} /></label>
          {tags.draft.reference_attachment_id && <div className="attachment-preview">
            <img src={attachmentUrl(roleId, tags.draft.reference_attachment_id)} alt="当前参考图" />
            <button type="button" aria-label="移除参考图" onClick={() => tags.patch({ reference_attachment_id: null })}>×</button>
          </div>}
          <label><input type="checkbox" checked={tags.draft.enabled} onChange={(event) => tags.patch({ enabled: event.target.checked })} />启用此标签</label>
        </fieldset>
        <button type="submit" disabled={tags.busy}>保存标签</button>
        {tags.editing && <button type="button" disabled={tags.busy} onClick={tags.resetDraft}>取消编辑标签</button>}
      </form>
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
    </section>}
  </>;
}
