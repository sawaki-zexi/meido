import type { Message, Role } from '../../api/types';

/** Resolve only owned illustrations. Channel moods never change desktop presentation. */
export function roleMood(role: Role, messages: Message[] = []) {
  const bindings = role.moodIllustrationBindings ?? {};
  const images = role.illustrations ?? [];
  const valid = (mood: unknown): mood is string => typeof mood === 'string' && images.some(image => image.id === bindings[mood]);
  let mood = valid(role.currentMood) ? role.currentMood : valid(role.defaultMood) ? role.defaultMood : Object.keys(bindings).find(valid) ?? '平静';
  const lastUpdated = Date.parse(role.currentMoodUpdatedAt ?? '') || 0;
  const latest = [...messages].reverse().find(message => message.sessionKey === `role:${role.id}` && message.role === 'assistant'
    && message.status === 'completed' && Date.parse(message.createdAt) > lastUpdated && valid(message.metadata?.mood));
  if (latest && valid(latest.metadata?.mood)) mood = latest.metadata.mood;
  return { mood, illustration: images.find(image => image.id === bindings[mood]) };
}
