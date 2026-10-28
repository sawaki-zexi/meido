import { useEffect, useState } from 'react';
import { IconButton } from '../../ui/Icon';
import { advanceStoryPlayback, createStoryPlaybackState, getNextStoryBeat, getPresentedStoryBeat, syncStoryPlaybackState } from './storyPlayback';
import { getStoryBeatPresentationFragments } from './storyBeatPresentation';
import { formatStoryDate } from './storyTime';
import { getStoryResourceErrorMessage, isStoryRoleInCurrentScene, selectActiveStoryVisualResource } from './selectors';
import type { StoryPreferences } from './storyPreferences';
import type { StoryDetails, StoryResource } from './types';

export function StoryGame({ story, preferences, busy, blocked, onInput, onContinue, onRetry, onArchive, onGallery, onSettings, onExit, onImage }: { story: StoryDetails; preferences: StoryPreferences; busy: boolean; blocked: boolean; onInput: (text: string) => Promise<boolean>; onContinue: () => void; onRetry: (turnId: string) => void; onArchive: () => void; onGallery: () => void; onSettings: () => void; onExit: () => void; onImage: (resource: StoryResource) => void }) {
  const [playback, setPlayback] = useState(() => createStoryPlaybackState(story));
  const [fragmentCursor, setFragmentCursor] = useState({ beatId: '', index: 0 });
  const [input, setInput] = useState('');
  const [visible, setVisible] = useState(true);
  const currentPlayback = syncStoryPlaybackState(playback, story);
  const beat = getPresentedStoryBeat(story, currentPlayback);
  const nextBeat = getNextStoryBeat(story, currentPlayback);
  const fragments = beat ? getStoryBeatPresentationFragments(beat) : [];
  const fragmentIndex = beat?.id === fragmentCursor.beatId ? Math.min(fragmentCursor.index, fragments.length - 1) : 0;
  const fragment = fragments[fragmentIndex];
  const hasNext = Boolean(nextBeat || fragmentIndex < fragments.length - 1);
  const working = busy || story.segment.operation === 'generating';
  const failedTurn = [...story.turns].reverse().find(turn => turn.status === 'failed' || turn.status === 'cancelled');
  const latestTurn = story.turns.at(-1);
  const retryableTurn = latestTurn && (latestTurn.status === 'failed' || latestTurn.status === 'cancelled') ? latestTurn : story.beats.length === 0 ? failedTurn : null;
  const canInput = story.beats.length > 0 && story.status === 'active' && story.segment.operation === 'awaiting_player' && !working && !blocked && !hasNext;
  const resource = selectActiveStoryVisualResource(story);
  const latestVisual = [...story.cgGallery, ...(story.backgroundResource ? [story.backgroundResource] : [])].sort((a, b) => a.sequence - b.sequence).at(-1);
  const backdrop = resource?.path || story.roleSnapshot.chatBackgroundUrl;
  const failureDetail = retryableTurn?.error && typeof retryableTurn.error === 'object' && 'message' in retryableTurn.error && typeof retryableTurn.error.message === 'string' ? retryableTurn.error.message : '';
  const character = story.roleSnapshot.illustrations?.find(Boolean) || story.roleSnapshot.avatar;
  const showCharacter = character && (!resource || resource.visualType === 'scene') && isStoryRoleInCurrentScene(story);
  const text = fragment?.text || (story.beats.length ? '' : story.background);
  const [reveal, setReveal] = useState({ key: '', count: 0 });
  const fragmentKey = `${beat?.id ?? 'opening'}:${fragmentIndex}`;
  const count = preferences.showFullText ? text.length : reveal.key === fragmentKey ? reveal.count : 0;
  const fullyVisible = count >= text.length;
  const advance = () => {
    if (!visible) { setVisible(true); return; }
    if (!fullyVisible) { setReveal({ key: fragmentKey, count: text.length }); return; }
    if (fragmentIndex < fragments.length - 1 && beat) setFragmentCursor({ beatId: beat.id, index: fragmentIndex + 1 });
    else if (nextBeat) { setFragmentCursor({ beatId: nextBeat.id, index: 0 }); setPlayback(advanceStoryPlayback(story, currentPlayback)); }
  };
  useEffect(() => {
    if (preferences.showFullText || fullyVisible || !visible) return;
    const delay = { slow: 65, normal: 30, fast: 10 }[preferences.textSpeed];
    const timer = setTimeout(() => setReveal({ key: fragmentKey, count: count + 1 }), delay);
    return () => clearTimeout(timer);
  }, [count, fullyVisible, fragmentKey, preferences.textSpeed, preferences.showFullText, visible]);
  useEffect(() => {
    if (!preferences.autoPlay || !fullyVisible || !hasNext || working || !visible) return;
    const timer = setTimeout(advance, preferences.autoPlayDelayMs);
    return () => clearTimeout(timer);
  }, [preferences.autoPlay, preferences.autoPlayDelayMs, fullyVisible, hasNext, working, visible, fragmentKey]);
  const submit = async () => { const text = input.trim(); if (!text || !canInput) return; if (await onInput(text)) setInput(''); };

  return <section className={`story-game story-motion-${preferences.reducedMotion ? 'reduced' : preferences.motionIntensity}`} aria-label={story.title} onContextMenu={event => { if ((event.target as Element).closest('input, textarea')) return; event.preventDefault(); setVisible(value => !value); }}>
    <div className="story-game-backdrop" style={backdrop ? { backgroundImage: `url(${backdrop})` } : undefined} />
    {showCharacter && <img key={`${story.currentScene.key}:${character}`} className="story-character" src={character} alt="" />}
    <header className="story-game-toolbar"><div><strong>{story.title}</strong><small>{formatStoryDate(story.currentStoryDate)} · {story.currentTimeBand} · {story.currentScene.name}</small></div><nav aria-label="剧情操作"><IconButton icon="knowledge" label="剧情记录" onClick={onArchive} /><IconButton icon="image" label="CG 图库" onClick={onGallery} /><IconButton icon="settings" label="剧情设置" onClick={onSettings} /><IconButton icon="back" label="返回剧情主菜单" onClick={onExit} /></nav></header>
    <button type="button" className="story-stage-advance" aria-label={visible ? '推进剧情文字' : '显示对话框'} onClick={advance} onWheel={event => { if (event.deltaY < 0) onArchive(); }} />
    {!visible && <button className="story-show-dialogue" type="button" onClick={() => setVisible(true)}>显示对话框</button>}
    {visible && <div className="story-dialogue">
      <div className="story-dialogue-label"><strong>{fragment?.kind === 'dialogue' ? beat?.speaker || story.roleSnapshot.name : '旁白'}</strong><span>{story.status === 'archived' ? '已归档 · 只读' : working ? '剧情生成中…' : hasNext ? '点击继续阅读' : '轮到你了'}</span><button type="button" className="ghost" onClick={() => setVisible(false)}>隐藏</button></div>
      <p className={`story-dialogue-text ${fragment?.kind === 'dialogue' ? '' : 'story-narration'}`} aria-live="off">{text.slice(0, count)}{!fullyVisible && <span aria-hidden="true">▏</span>}</p>
      <div className="story-actions">{(!fullyVisible || hasNext) && <button type="button" onClick={advance}>{fullyVisible ? '下一段' : '显示全文'}</button>}{resource?.kind === 'cg' && <button type="button" disabled={busy || blocked || resource.status === 'generating'} onClick={() => onImage(resource)}>重新生成当前 CG</button>}</div>
      {retryableTurn && !working && <div className="story-failure" role="alert"><span>{failureDetail || (retryableTurn.status === 'cancelled' ? '这次剧情已中断，可以继续重试。' : '这次剧情未生成完成。')}</span><button type="button" disabled={busy || blocked || story.status !== 'active'} onClick={() => onRetry(retryableTurn.id)}>重试这次剧情</button></div>}
      {latestVisual?.status === 'failed' && (story.generateImages || latestVisual.errorCode !== 'images_not_enabled') && <div className="story-failure"><span>{getStoryResourceErrorMessage(latestVisual.errorCode)}</span><button type="button" onClick={onGallery}>查看图库</button></div>}
      {canInput && <form className="story-input" onSubmit={event => { event.preventDefault(); void submit(); }}>
        <label className="sr-only" htmlFor="story-player-input">你的行动或回应</label><textarea id="story-player-input" rows={2} maxLength={8000} placeholder="写下你的行动、回应，或希望剧情发展的方向…" value={input} onChange={event => setInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }} />
        <div className="story-actions"><small>{story.generateImages ? '本故事已允许自动生图；每次行动最多 1 张，可能产生费用。' : '剧情自动保存'}</small><button type="button" onClick={onContinue}>继续故事</button><button type="submit" disabled={!input.trim()}>发送行动</button></div>
      </form>}
    </div>}
  </section>;
}
