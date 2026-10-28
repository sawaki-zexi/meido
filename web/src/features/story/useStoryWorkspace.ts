import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, errorMessage } from '../../api/client';
import { createStoryCommand, getStory, getStoryCapabilities, listStories, listStoryGallery, storyCommand, storyRequestId, type StoryCapabilities, type StoryCommand } from '../../api/story';
import type { StoryCgGallery, StoryCreationInput, StoryDetails, StoryResource, StorySummary } from './types';
import { createInitialStoryCreationInput } from './storyCreationWizard';

type Pending = { command: StoryCommand; label: string };
const draftKey = 'meido:story-draft:v1';
const pendingKey = 'meido:story-pending:v1';
function readSaved<T>(key: string): T | null { try { return JSON.parse(localStorage.getItem(key) ?? 'null') as T | null; } catch { return null; } }
function save(key: string, value: unknown) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, JSON.stringify(value)); } catch { /* Storage is optional; requests still retain their in-memory identity. */ } }
function mergeResource(previous: StoryResource | undefined | null, incoming: StoryResource) { return previous && Date.parse(previous.updatedAt) > Date.parse(incoming.updatedAt) ? previous : incoming; }
function mergeStory(previous: StoryDetails | null, incoming: StoryDetails): StoryDetails {
  if (!previous || previous.id !== incoming.id) return incoming;
  const base = previous.revision > incoming.revision ? previous : incoming;
  const resources = new Map(previous.cgGallery.map(item => [item.id, item]));
  for (const item of incoming.cgGallery) resources.set(item.id, mergeResource(resources.get(item.id), item));
  return { ...base, cgGallery: [...resources.values()].sort((a, b) => a.sequence - b.sequence), backgroundResource: incoming.backgroundResource ? mergeResource(previous.backgroundResource, incoming.backgroundResource) : previous.backgroundResource };
}
export function isStoryWorking(story: StoryDetails | null) { return Boolean(story && (story.segment.operation === 'generating' || story.cgGallery.some(item => item.status === 'generating') || story.backgroundResource?.status === 'generating')); }

/** Owns Story requests without coupling narrative state to the ordinary chat session. */
export function useStoryWorkspace() {
  const [stories, setStories] = useState<StorySummary[]>([]);
  const [gallery, setGallery] = useState<StoryCgGallery[]>([]);
  const [story, setStory] = useState<StoryDetails | null>(null);
  const [capabilities, setCapabilities] = useState<StoryCapabilities | null>(null);
  const [draft, setDraftState] = useState<StoryCreationInput>(() => {
    const saved = readSaved<StoryCreationInput>(draftKey);
    return saved && saved.playerProfile && typeof saved.title === 'string' ? saved : createInitialStoryCreationInput();
  });
  const [pending, setPendingState] = useState<Pending | null>(() => readSaved<Pending>(pendingKey));
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  const locked = useRef(false);
  const selection = useRef(0);
  const setPending = (next: Pending | null) => { setPendingState(next); save(pendingKey, next); };
  const setDraft = (value: StoryCreationInput) => { setDraftState(value); save(draftKey, value); };
  const acceptStory = useCallback((next: StoryDetails) => {
    setStory(previous => mergeStory(previous, next));
    setGallery(previous => previous.map(group => group.storyId === next.id ? { ...group, items: next.cgGallery, status: next.status } : group));
    setStories(previous => previous.map(item => item.storyId === next.id ? { ...item, status: next.status, currentStoryDate: next.currentStoryDate, currentTimeBand: next.currentTimeBand, currentScene: next.currentScene } : item));
  }, []);
  const refresh = useCallback(async () => {
    try {
      const [list, groups] = await Promise.all([listStories(), listStoryGallery()]);
      if (mounted.current) { setStories(list); setGallery(groups); }
    } catch (cause) { if (mounted.current) setError(errorMessage(cause, '无法读取剧情存档')); }
    finally { if (mounted.current) setLoading(false); }
  }, []);
  useEffect(() => { mounted.current = true; void refresh(); return () => { mounted.current = false; selection.current++; }; }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    setCapabilities(null);
    void getStoryCapabilities(draft.roleId || undefined).then(value => { if (!cancelled) setCapabilities(value); }).catch(cause => { if (!cancelled) setError(errorMessage(cause, '无法读取图片许可')); });
    return () => { cancelled = true; };
  }, [draft.roleId]);

  const load = useCallback(async (id: string) => {
    const ticket = ++selection.current;
    setError(''); setBusy(true);
    try { const next = await getStory(id); if (!mounted.current || selection.current !== ticket) return false; setStory(next); return true; }
    catch (cause) { if (mounted.current && selection.current === ticket) setError(errorMessage(cause, '无法读取这个剧情')); return false; }
    finally { if (mounted.current && selection.current === ticket) setBusy(false); }
  }, []);

  const working = isStoryWorking(story);
  useEffect(() => {
    if (!working || !story) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const id = story.id;
    const poll = async () => {
      try { const next = await getStory(id); if (!cancelled) acceptStory(next); }
      catch (cause) { if (!cancelled) setError(errorMessage(cause, '剧情状态暂时无法读取；不会重复提交生成')); }
      if (!cancelled) timer = setTimeout(poll, 1200);
    };
    timer = setTimeout(poll, 900);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [story?.id, working, acceptStory]);

  const galleryWorking = gallery.some(group => group.items.some(item => item.status === 'generating'));
  useEffect(() => {
    if (!galleryWorking) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { const next = await listStoryGallery(); if (!cancelled) setGallery(next); }
      catch (cause) { if (!cancelled) setError(errorMessage(cause, '图库状态暂时无法读取')); }
      if (!cancelled) timer = setTimeout(poll, 1800);
    };
    timer = setTimeout(poll, 1800);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [galleryWorking]);

  const execute = async (request: Pending) => {
    if (locked.current) return null;
    locked.current = true; setBusy(true); setError(''); setPending(request);
    try {
      const response = await storyCommand<{ story: StoryDetails }>(request.command);
      if (!mounted.current) return null;
      acceptStory(response.story); setPending(null); void refresh();
      return response.story;
    } catch (cause) {
      if (mounted.current) {
        setError(errorMessage(cause, '操作未完成，请先查看状态再重试'));
        // A rejected request did not execute; transport failures retain the exact identity.
        if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
          setPending(null);
          const id = request.command.payload.story_id;
          if (typeof id === 'string') void getStory(id).then(next => { if (mounted.current) acceptStory(next); }).catch(() => {});
        }
      }
      return null;
    } finally { locked.current = false; if (mounted.current) setBusy(false); }
  };
  const mutate = (method: string, payload: Record<string, unknown>, label: string) => execute({ command: { method, payload, request_id: storyRequestId() }, label });
  const create = () => execute({ command: createStoryCommand(draft, storyRequestId()), label: '创建剧情' });
  const input = (text: string) => story ? mutate('stories.input', { story_id: story.id, input: text, expected_revision: story.revision }, '提交行动') : Promise.resolve(null);
  const continueStory = () => story ? mutate('stories.continue', { story_id: story.id, expected_revision: story.revision }, '继续剧情') : Promise.resolve(null);
  const retryTurn = (turnId: string) => story ? mutate('stories.retry', { story_id: story.id, turn_id: turnId, expected_revision: story.revision }, '重试剧情') : Promise.resolve(null);
  const archive = (id: string, archived: boolean) => mutate('stories.archive', { story_id: id, archived }, archived ? '归档剧情' : '恢复剧情');
  const image = (id: string, resourceId: string, regenerate: boolean, novelaiRevision: number) => mutate(regenerate ? 'stories.cg.regenerate' : 'stories.cg.retry', { story_id: id, resource_id: resourceId, expected_novelai_revision: novelaiRevision }, regenerate ? '重新生成图片' : '重试图片');
  return { stories, gallery, story, capabilities, draft, setDraft, pending, busy, loading, error, setError, refresh, load, create, input, continueStory, retryTurn, archive, image, retryPending: () => pending ? execute(pending) : Promise.resolve(null), clearDraft: () => setDraft(createInitialStoryCreationInput()) };
}
