import { useEffect, useRef } from 'react';
import { buildStoryArchiveDays } from './storyArchivePresentation';
import type { StoryDetails } from './types';

export function StoryArchive({ story, onBack }: { story: StoryDetails; onBack: () => void }) {
  const bottom = useRef<HTMLDivElement>(null);
  const days = buildStoryArchiveDays(story);
  useEffect(() => { bottom.current?.scrollIntoView?.({ block: 'nearest' }); }, [story.id]);
  return <section className="story-archive"><header className="story-page-header"><button type="button" onClick={onBack}>返回剧情</button><h1>剧情记录 · {story.title}</h1></header><div className="story-archive-body"><p className="story-preserve muted">{story.background}</p>{days.map(day => <section key={day.key}><h2>{day.label}</h2>{day.periods.map(period => <section key={period.timeBand}><h3>{period.timeBand}</h3><ol>{period.entries.map(entry => <li key={entry.id} className={`story-archive-${entry.kind}`}><small>{entry.label}</small><p>{entry.text}</p></li>)}</ol></section>)}</section>)}{!days.length && <p>还没有已完成的剧情。</p>}<div ref={bottom} /></div></section>;
}
