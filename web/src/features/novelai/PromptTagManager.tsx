import type { PromptTagRating } from '../../api/novelai';
import { Notice, Placeholder } from '../../ui/Status';
import { usePromptTags } from './usePromptTags';

export function PromptTagManager({ roleId }: { roleId: string }) {
  const tags = usePromptTags(roleId);
  return <>
    <button type="button" aria-expanded={tags.open} onClick={tags.toggle}>{tags.open ? '收起提示标签' : '管理提示标签'}</button>
    {tags.open && <section aria-label="本角色提示标签库">
      <h3>本角色提示标签库</h3>
      <p>最多 250 个标签。管理与预览只在本地进行；实际匹配取决于角色已保存的标签许可、启用状态和 rating 许可。</p>
      {tags.error && <Notice onDismiss={tags.dismissError}>{tags.error}</Notice>}
      {!tags.page && !tags.error && <Placeholder loading>正在加载提示标签…</Placeholder>}
      {tags.page && <p>标签库版本 {tags.page.revision} · 已加载 {tags.page.tags.length} 个</p>}
      {tags.page?.tags.length === 0 && <Placeholder>暂无提示标签。</Placeholder>}
      {tags.page?.tags.map((tag) => <article key={tag.id} aria-label={`提示标签 ${tag.name}`}>
        <h4>{tag.name}</h4><p>{tag.enabled ? '已启用' : '已停用'} · {tag.rating} · 关键词：{tag.keywords.join('、')}</p>
        <button type="button" disabled={tags.busy} aria-label={`编辑标签 ${tag.name}`} onClick={() => tags.edit(tag)}>编辑</button>
        <button type="button" disabled={tags.busy} aria-label={`删除标签 ${tag.name}`} onClick={() => void tags.remove(tag)}>删除</button>
      </article>)}
      {tags.page?.nextCursor && <button type="button" disabled={tags.busy} onClick={() => void tags.more()}>加载更多标签</button>}
      <button type="button" disabled={tags.busy} onClick={() => void tags.reload()}>刷新标签库</button>
      <form aria-label={tags.editing ? '编辑提示标签' : '创建提示标签'} onSubmit={(event) => { event.preventDefault(); void tags.save(); }}>
        <fieldset disabled={tags.busy}>
          <legend>{tags.editing ? '编辑提示标签' : '创建提示标签'}</legend>
          <label className="field"><span>标签名称</span><input aria-label="标签名称" required maxLength={80} value={tags.draft.name} onChange={(event) => tags.patch({ name: event.target.value })} /></label>
          <label className="field"><span>匹配关键词（每行一个）</span><textarea aria-label="匹配关键词（每行一个）" required value={tags.keywords} onChange={(event) => tags.setKeywords(event.target.value)} /><small>1–16 个关键词，每个最多 80 字符；按文字匹配，不使用正则。</small></label>
          <label className="field"><span>标签正向 tags</span><textarea aria-label="标签正向 tags" maxLength={4000} value={tags.draft.prompt} onChange={(event) => tags.patch({ prompt: event.target.value })} /></label>
          <label className="field"><span>标签负向 tags</span><textarea aria-label="标签负向 tags" maxLength={4000} value={tags.draft.negative_prompt} onChange={(event) => tags.patch({ negative_prompt: event.target.value })} /></label>
          <label className="field"><span>标签 rating</span><select aria-label="标签 rating" value={tags.draft.rating} onChange={(event) => tags.patch({ rating: event.target.value as PromptTagRating })}>
            <option value="general">general</option><option value="mature">mature</option><option value="explicit">explicit</option></select></label>
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
