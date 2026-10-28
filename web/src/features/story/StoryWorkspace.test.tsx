import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { role } from '../../test/fakeBackend';
import type { StoryCommand } from '../../api/story';
import type { StoryBeat, StoryDetails } from './types';
import { StoryWorkspace } from './StoryWorkspace';

function beat(id: string, sequence: number, text: string, turnId = 'opening'): StoryBeat { return { id, sequence, text, turnId, storyId: 'story-1', segmentId: 'segment-1', storyDate: '2026-08-02', timeBand: '上午', kind: 'dialogue', speaker: '诗织', recordedAt: '2026-08-02T10:00:00Z' }; }
function fixture(): StoryDetails { return { id: 'story-1', title: '雨港', background: '在雨港相遇', status: 'active', revision: 1, generateImages: false, roleSnapshot: { id: 'r1', name: '诗织' }, playerProfile: { display_name: '岚', appearance: '短发', identity: '旅行者' }, segment: { id: 'segment-1', sequence: 1, storyDate: '2026-08-02', timeBand: '上午', status: 'active', mode: 'plot', operation: 'awaiting_player', openingContext: {}, runtimeSnapshot: {} }, beats: [beat('beat-1', 1, '你终于来了。')], cues: [], turns: [], currentScene: { key: 'port', name: '雨港', characterIds: ['r1', 'player'] }, backgroundResource: null, cgGallery: [{ id: 'image-1', storyId: 'story-1', kind: 'cg', visualType: 'character', sceneKey: 'port', status: 'ready', path: '/api/stories/story-1/resources/image-1/image', prompt: '1girl, harbor', sourceTurnId: 'opening', sequence: 1, errorCode: null, createdAt: '2026-08-02T10:00:00Z', updatedAt: '2026-08-02T10:00:00Z' }], currentStoryDate: '2026-08-02', currentTimeBand: '上午' }; }
function backend(initial = false, failInput = false) {
  let story = fixture(); let exists = initial; let failed = false;
  const mutations: StoryCommand[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (String(path).startsWith('/api/stories/capabilities')) return new Response(JSON.stringify({ imageEnabled: true, imageReason: '', novelaiRevision: 7 }));
    const command = JSON.parse(String(init?.body)) as StoryCommand;
    let data: unknown;
    if (command.method === 'stories.list') data = { stories: exists ? [{ story_id: story.id, relative_db_path: 'story-1/story.db', title: story.title, status: story.status, created_at: '2026-08-02T10:00:00Z', current_story_date: story.currentStoryDate, current_time_band: story.currentTimeBand, current_scene: { key: story.currentScene.key, name: story.currentScene.name, character_ids: story.currentScene.characterIds } }] : [] };
    else if (command.method === 'stories.cg.list') data = { stories: exists ? [{ story_id: story.id, title: story.title, status: story.status, created_at: '2026-08-02T10:00:00Z', items: story.cgGallery }] : [] };
    else if (command.method === 'stories.get') data = { story };
    else {
      mutations.push(command);
      if (command.method === 'stories.input' && failInput && !failed) { failed = true; throw new Error('Connection reset'); }
      if (command.method === 'stories.create') { exists = true; story = { ...story, generateImages: command.payload.generate_images === true }; }
      if (command.method === 'stories.input') story = { ...story, revision: 2, beats: [...story.beats, beat('beat-2', 2, '我们去灯塔。', 'turn-2'), beat('beat-3', 3, '她向你伸出手。', 'turn-2')], turns: [{ id: 'turn-2', kind: 'player', input: String(command.payload.input), status: 'committed', attemptId: 'attempt-2', committedBeatIds: ['beat-2', 'beat-3'], error: null, createdAt: '', updatedAt: '' }] };
      if (command.method === 'stories.archive') story = { ...story, status: command.payload.archived ? 'archived' : 'active' };
      data = { story };
    }
    return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  return mutations;
}
function mount() { localStorage.setItem('meido:story-preferences:v1', JSON.stringify({ showFullText: true })); return render(<StoryWorkspace roles={[role({ id: 'r1', name: '诗织', description: '女仆' })]} onExit={() => {}} onOpenModelSettings={() => {}} onOpenImageSettings={() => {}} />); }
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

it('creates a save, presents every new beat, reopens transcript and confirms a separate CG attempt', async () => {
  const calls = backend(); const user = userEvent.setup(); mount();
  await user.click(screen.getByRole('button', { name: '新建剧情' }));
  await user.click(screen.getByRole('button', { name: '诗织 女仆' }));
  await user.click(screen.getByRole('button', { name: '下一步' }));
  await user.type(screen.getByLabelText('剧情标题'), '雨港');
  await user.type(screen.getByLabelText('世界背景与开场方向'), '在雨港相遇');
  fireEvent.change(screen.getByLabelText('故事日期'), { target: { value: '2026-08-02' } });
  await user.selectOptions(screen.getByLabelText('开场时段'), '上午');
  await user.click(screen.getByRole('button', { name: '下一步' }));
  await user.type(screen.getByLabelText('称呼'), '岚');
  await user.type(screen.getByLabelText('身份与角色关系'), '旅行者');
  await user.type(screen.getByLabelText('外观'), '短发');
  await user.click(screen.getByRole('button', { name: '下一步' }));
  await user.click(screen.getByRole('button', { name: '开始故事' }));
  expect(await screen.findByText('你终于来了。')).toBeInTheDocument();
  expect(calls[0].payload.generate_images).toBe(true);
  await user.type(screen.getByLabelText('你的行动或回应'), '去灯塔');
  await user.click(screen.getByRole('button', { name: '发送行动' }));
  expect(await screen.findByText('我们去灯塔。')).toBeInTheDocument();
  expect(screen.queryByLabelText('你的行动或回应')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '下一段' }));
  expect(screen.getByText('她向你伸出手。')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '剧情记录' }));
  expect(await screen.findByText('去灯塔')).toBeInTheDocument();
  expect(screen.getByText('我们去灯塔。')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '返回剧情' }));
  await user.click(screen.getByRole('button', { name: 'CG 图库' }));
  await user.click(await screen.findByRole('button', { name: '重新生成' }));
  const confirm = await screen.findByRole('button', { name: '确认生成' });
  expect(calls.filter(call => call.method.startsWith('stories.cg.'))).toHaveLength(0);
  await waitFor(() => expect(confirm).toBeEnabled());
  await user.click(confirm);
  await waitFor(() => expect(calls.some(call => call.method === 'stories.cg.regenerate' && call.payload.expected_novelai_revision === 7)).toBe(true));
  await user.click(screen.getByRole('button', { name: '返回' }));
  await user.click(screen.getByRole('button', { name: '返回剧情主菜单' }));
  await user.click(screen.getByRole('button', { name: '载入存档' }));
  await user.click(await screen.findByRole('button', { name: /雨港 2026年8月2日/ }));
  expect(await screen.findByText('她向你伸出手。')).toBeInTheDocument();
});

it('retains the same request identity after an uncertain input instead of creating a second turn', async () => {
  const calls = backend(true, true); const user = userEvent.setup(); mount();
  await user.click(screen.getByRole('button', { name: '载入存档' }));
  await user.click(await screen.findByRole('button', { name: /雨港 2026年8月2日/ }));
  await user.type(screen.getByLabelText('你的行动或回应'), '去灯塔');
  await user.click(screen.getByRole('button', { name: '发送行动' }));
  await user.click(await screen.findByRole('button', { name: '重查原请求' }));
  await screen.findByText('我们去灯塔。');
  const inputs = calls.filter(call => call.method === 'stories.input');
  expect(inputs).toHaveLength(2);
  expect(inputs[1]).toEqual(inputs[0]);
  expect(localStorage.getItem('meido:story-pending:v1')).toBeNull();
});
