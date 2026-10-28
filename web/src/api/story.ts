import { api } from './client';
import type { StoryCgGallery, StoryCreationInput, StoryDetails, StorySummary } from '../features/story/types';

type SummaryPayload = { story_id: string; relative_db_path: string; title: string; status: StorySummary['status']; created_at: string; current_story_date: string; current_time_band: StorySummary['currentTimeBand']; current_scene: { key: string; name: string; character_ids: string[] } };
export type StoryCommand = { method: string; payload: Record<string, unknown>; request_id: string };
export const storyRequestId = () => crypto.randomUUID();

/** Request identities survive retries; never silently create another paid operation. */
export function storyCommand<T>(command: StoryCommand): Promise<T> {
  return api<T>('/api/stories/commands', { method: 'POST', body: JSON.stringify(command) });
}
const read = <T>(method: string, payload: Record<string, unknown> = {}) => storyCommand<T>({ method, payload, request_id: storyRequestId() });
export async function listStories(includeArchived = true): Promise<StorySummary[]> {
  const response = await read<{ stories: SummaryPayload[] }>('stories.list', { include_archived: includeArchived });
  return response.stories.map(story => ({ storyId: story.story_id, relativeDbPath: story.relative_db_path, title: story.title, status: story.status, createdAt: story.created_at, currentStoryDate: story.current_story_date, currentTimeBand: story.current_time_band, currentScene: { key: story.current_scene.key, name: story.current_scene.name, characterIds: story.current_scene.character_ids } }));
}
export async function getStory(storyId: string) {
  return (await read<{ story: StoryDetails }>('stories.get', { story_id: storyId })).story;
}
export async function listStoryGallery(): Promise<StoryCgGallery[]> {
  const response = await read<{ stories: { story_id: string; title: string; status: StoryCgGallery['status']; created_at: string; items: StoryCgGallery['items'] }[] }>('stories.cg.list');
  return response.stories.map(story => ({ storyId: story.story_id, title: story.title, status: story.status, createdAt: story.created_at, items: story.items }));
}
export function createStoryCommand(input: StoryCreationInput, creationId: string): StoryCommand {
  return { method: 'stories.create', request_id: creationId, payload: { creation_id: creationId, title: input.title, background: input.background, story_date: input.storyDate, time_band: input.timeBand, role_id: input.roleId, generate_images: input.generateImages !== false, expected_novelai_revision: input.novelaiRevision, player_profile: { display_name: input.playerProfile.displayName, appearance: input.playerProfile.appearance, identity: input.playerProfile.identity } } };
}
export function storyResourceUrl(storyId: string, resourceId: string) {
  return `/api/stories/${encodeURIComponent(storyId)}/resources/${encodeURIComponent(resourceId)}/image`;
}

export type StoryCapabilities = { imageEnabled: boolean; imageReason: string; novelaiRevision: number };
export const getStoryCapabilities = (roleId?: string) => api<StoryCapabilities>(`/api/stories/capabilities${roleId ? `?role_id=${encodeURIComponent(roleId)}` : ''}`);
