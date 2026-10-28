import { useRef, useState } from 'react';
import type { Role } from '../../api/types';
import { getStory, getStoryCapabilities, type StoryCapabilities } from '../../api/story';
import { errorMessage } from '../../api/client';
import { Dialog } from '../../ui/Dialog';
import { Icon } from '../../ui/Icon';
import { Notice, Placeholder } from '../../ui/Status';
import { StoryCreateFlow } from './StoryCreateFlow';
import { StoryGame } from './StoryGame';
import { StoryArchive } from './StoryArchive';
import { StoryGallery } from './StoryGallery';
import { StorySettings } from './StorySettings';
import { readStoryPreferences, writeStoryPreferences } from './storyPreferences';
import { formatStoryDate } from './storyTime';
import { useStoryWorkspace } from './useStoryWorkspace';
import type { StoryResource } from './types';
import './story.css';

type Page = 'menu' | 'load' | 'create' | 'game' | 'archive' | 'gallery';
type Props = { roles: Role[]; onExit: () => void; onOpenModelSettings: () => void; onOpenImageSettings: () => void };

/** Native Meido Story entry, persisted saves, and visual-novel presentation. */
export function StoryWorkspace({ roles, onExit, onOpenModelSettings, onOpenImageSettings }: Props) {
  const state = useStoryWorkspace();
  const [page, setPage] = useState<Page>('menu');
  const [galleryReturn, setGalleryReturn] = useState<Page>('menu');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [preferences, setPreferences] = useState(readStoryPreferences);
  const [showArchived, setShowArchived] = useState(false);
  const [imageChoice, setImageChoice] = useState<{ storyId: string; resource: StoryResource } | null>(null);
  const [imageLimits, setImageLimits] = useState<StoryCapabilities | null>(null);
  const [imageError, setImageError] = useState('');
  const navigation = useRef(0);
  const imageTicket = useRef(0);
  const navigate = (next: Page) => { navigation.current++; setPage(next); };
  const openStory = async (id: string) => {
    const ticket = ++navigation.current;
    if (await state.load(id) && ticket === navigation.current) setPage('game');
  };
  const create = async () => {
    const ticket = ++navigation.current;
    const story = await state.create();
    if (story && ticket === navigation.current) setPage('game');
  };
  const openGallery = (origin: Page) => { setGalleryReturn(origin); navigate('gallery'); void state.refresh(); };
  const openImage = async (storyId: string, resource: StoryResource) => {
    const ticket = ++imageTicket.current;
    setImageChoice({ storyId, resource }); setImageLimits(null); setImageError('');
    try {
      const story = state.story?.id === storyId ? state.story : await getStory(storyId);
      const limits = await getStoryCapabilities(story.roleSnapshot.id);
      if (imageTicket.current === ticket) setImageLimits(limits);
    } catch (cause) { if (imageTicket.current === ticket) setImageError(errorMessage(cause, '无法读取图片许可')); }
  };
  const closeImage = () => { if (state.busy) return; imageTicket.current++; setImageChoice(null); };
  const confirmImage = async () => {
    if (!imageChoice || !imageLimits || state.busy || state.pending) return;
    const result = await state.image(imageChoice.storyId, imageChoice.resource.id, imageChoice.resource.status !== 'failed', imageLimits.novelaiRevision);
    if (result) { imageTicket.current++; setImageChoice(null); }
  };
  const retryPending = async () => { const story = await state.retryPending(); if (story && page === 'create') navigate('game'); };
  const list = state.stories.filter(item => showArchived || item.status !== 'archived');
  const errors = <>{state.error && <Notice onDismiss={() => state.setError('')}>{state.error}</Notice>}{state.pending && !state.busy && <div className="story-pending" role="status"><span>“{state.pending.label}”的结果尚未确认。先查看存档／图库；重查会复用原请求，不会另起一次操作。</span><button type="button" onClick={() => void retryPending()}>重查原请求</button></div>}</>;
  const controlsBlocked = state.busy || Boolean(state.pending);

  return <div className="story-workspace">
    <div className="story-notices">{errors}</div>
    {page === 'menu' && <section className="story-menu"><div className="story-menu-heading"><p className="eyebrow">STORY</p><h1>故事模式</h1><p>与她一起，走进下一段故事。</p></div><nav aria-label="故事主菜单"><button type="button" disabled={controlsBlocked || !roles.length} onClick={() => navigate('create')}><Icon name="plus" />新建剧情</button>{state.story && <button type="button" onClick={() => navigate('game')}><Icon name="chat" />返回 {state.story.title}</button>}<button type="button" disabled={state.busy} onClick={() => { navigate('load'); void state.refresh(); }}><Icon name="save" />载入存档</button><button type="button" disabled={state.busy} onClick={() => openGallery('menu')}><Icon name="image" />CG 图库</button><button type="button" onClick={() => setSettingsOpen(true)}><Icon name="settings" />剧情设置</button><button type="button" onClick={onExit}><Icon name="back" />返回对话</button></nav>{!roles.length && <p className="muted">请先在角色页创建一位角色。</p>}</section>}
    {page === 'load' && <section className="story-load"><header className="story-page-header"><button type="button" onClick={() => navigate('menu')}>返回主菜单</button><h1>载入存档</h1><button type="button" disabled={state.busy} onClick={() => void state.refresh()}>刷新</button></header><div className="story-load-body"><label className="checkbox-field"><input type="checkbox" checked={showArchived} onChange={event => setShowArchived(event.target.checked)} />显示已归档故事</label>{state.loading ? <Placeholder loading>正在读取存档…</Placeholder> : !list.length ? <Placeholder>还没有已保存的剧情。</Placeholder> : <ul className="story-save-list">{list.map(item => <li key={item.storyId}><button type="button" disabled={state.busy} className="story-save-open" onClick={() => void openStory(item.storyId)}><strong>{item.title}</strong><small>{formatStoryDate(item.currentStoryDate)} · {item.currentTimeBand} · {item.currentScene.name}{item.status === 'archived' ? ' · 已归档' : ''}</small></button><button type="button" disabled={controlsBlocked} onClick={() => void state.archive(item.storyId, item.status !== 'archived')}>{item.status === 'archived' ? '恢复' : '归档'}</button></li>)}</ul>}<p className="field-hint">完成的剧情自动保存在本地。归档不会删除剧情或图片，可随时恢复。</p></div></section>}
    {page === 'create' && <StoryCreateFlow roles={roles} value={state.draft} onChange={state.setDraft} busy={controlsBlocked} capabilities={state.capabilities} onSubmit={() => void create()} onBack={() => navigate('menu')} />}
    {page === 'game' && state.story && <StoryGame key={state.story.id} story={state.story} preferences={preferences} busy={state.busy} blocked={Boolean(state.pending)} onInput={async text => Boolean(await state.input(text))} onContinue={() => void state.continueStory()} onRetry={id => void state.retryTurn(id)} onArchive={() => navigate('archive')} onGallery={() => openGallery('game')} onSettings={() => setSettingsOpen(true)} onExit={() => navigate('menu')} onImage={resource => void openImage(state.story!.id, resource)} />}
    {page === 'archive' && state.story && <StoryArchive story={state.story} onBack={() => navigate('game')} />}
    {page === 'gallery' && <StoryGallery groups={state.gallery} busy={controlsBlocked} onBack={() => navigate(galleryReturn)} onImage={(id, resource) => void openImage(id, resource)} onOpenStory={id => void openStory(id)} />}
    {settingsOpen && <StorySettings value={preferences} onChange={next => { setPreferences(next); writeStoryPreferences(next); }} onClose={() => setSettingsOpen(false)} onOpenModelSettings={() => { setSettingsOpen(false); onOpenModelSettings(); }} onOpenImageSettings={() => { setSettingsOpen(false); onOpenImageSettings(); }} />}
    {imageChoice && <Dialog title={imageChoice.resource.status === 'failed' ? '重试剧情图片' : '重新生成剧情图片'} modalFocus onClose={closeImage}><div className="story-image-confirm"><p>这会提交一次新的 NovelAI 生图尝试，可能产生费用。已有图片和剧情记录会保留，不会向聊天或渠道发送图片。</p>{imageChoice.resource.generation && <p className="field-hint">上次尝试：{imageChoice.resource.generation.status}；消费：{imageChoice.resource.generation.consumption}。结果未知时，可能已经消费，请勿当作免费重试。</p>}{!imageLimits && !imageError && <Placeholder loading>正在读取 NovelAI 设置…</Placeholder>}{imageLimits && !imageLimits.imageEnabled && <Notice>{imageLimits.imageReason || '当前生图许可不可用'}</Notice>}{(imageError || state.error) && <Notice>{imageError || state.error}</Notice>}<div className="story-actions"><button type="button" disabled={state.busy} onClick={closeImage}>取消</button><button type="button" disabled={controlsBlocked || !imageLimits?.imageEnabled} onClick={() => void confirmImage()}>{state.busy ? '提交中…' : '确认生成'}</button></div></div></Dialog>}
  </div>;
}
