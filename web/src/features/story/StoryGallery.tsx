import { useState } from 'react';
import { Dialog } from '../../ui/Dialog';
import { Placeholder } from '../../ui/Status';
import { getStoryResourceErrorMessage } from './selectors';
import type { StoryCgGallery, StoryResource } from './types';

export function StoryGallery({ groups, busy, onBack, onImage, onOpenStory }: { groups: StoryCgGallery[]; busy: boolean; onBack: () => void; onImage: (storyId: string, resource: StoryResource) => void; onOpenStory: (id: string) => void }) {
  const [selected, setSelected] = useState<{ title: string; resource: StoryResource } | null>(null);
  const [filter, setFilter] = useState('');
  const visible = groups.filter(group => !filter || group.storyId === filter);
  return <section className="story-gallery"><header className="story-page-header"><button type="button" onClick={onBack}>返回</button><h1>CG 图库</h1><label><span className="sr-only">按故事筛选</span><select aria-label="按故事筛选" value={filter} onChange={event => setFilter(event.target.value)}><option value="">全部故事</option>{groups.map(group => <option key={group.storyId} value={group.storyId}>{group.title}</option>)}</select></label></header>
    <div className="story-gallery-body">{visible.map(group => <section key={group.storyId}><h2><button className="ghost" type="button" disabled={busy} onClick={() => onOpenStory(group.storyId)}>{group.title}</button>{group.status === 'archived' && <small>已归档</small>}</h2><div className="story-gallery-grid">{group.items.map(resource => <article className="story-cg-card" key={resource.id}>
      {resource.path ? <button type="button" className="story-cg-preview" onClick={() => setSelected({ title: group.title, resource })}><img src={resource.path} alt={`${group.title} ${resource.kind === 'background' ? '开场背景' : 'CG'} ${resource.sequence}`} loading="lazy" /></button> : <div className="story-cg-placeholder">{resource.status === 'generating' ? <Placeholder loading>图片生成中…</Placeholder> : '暂无图片'}</div>}
      <div className="story-cg-meta"><strong>{resource.kind === 'background' ? '开场背景' : resource.visualType === 'character' ? '人物 CG' : '场景 CG'}</strong><span>{resource.status === 'ready' ? '已保存' : resource.status === 'generating' ? '生成中' : getStoryResourceErrorMessage(resource.errorCode)}</span>
      {resource.generation && <p className="field-hint">尝试 {resource.generation.attemptId} · {resource.generation.status} · 消费 {resource.generation.consumption}{resource.generation.errorCode ? ` · ${resource.generation.errorCode}` : ''}</p>}
      <details><summary>图片提示词</summary><p className="story-preserve">{resource.prompt}</p></details><div className="story-actions">{resource.path && <a href={resource.path} target="_blank" rel="noreferrer">查看原图</a>}{(resource.kind === 'cg' || resource.status === 'failed') && <button type="button" disabled={busy || resource.status === 'generating'} onClick={() => onImage(group.storyId, resource)}>{resource.status === 'failed' ? '重试图片' : '重新生成'}</button>}</div></div>
    </article>)}</div>{!group.items.length && <p className="muted">这个故事尚无图片。</p>}</section>)}{!groups.length && <Placeholder>还没有剧情图片。</Placeholder>}</div>
    {selected && <Dialog title={`${selected.title} · CG`} onClose={() => setSelected(null)} modalFocus><img className="story-lightbox" src={selected.resource.path ?? ''} alt={`${selected.title} CG`} /></Dialog>}
  </section>;
}
