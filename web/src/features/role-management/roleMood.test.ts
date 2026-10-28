import { expect, it } from 'vitest';
import { role } from '../../test/fakeBackend';
import { roleMood } from './roleMood';
import type { Message } from '../../api/types';

it('presents current desktop mood, ignores channel/failed/old events, and falls back to owned default', () => {
  const calm = { id: 'i1', name: 'calm', url: '/calm', mediaType: 'image/png', size: 10, category: '未分类' };
  const happy = { ...calm, id: 'i2', name: 'happy', url: '/happy' };
  const owner = role({ id: 'r1', name: '诗织', illustrations: [calm, happy], moodIllustrationBindings: { 平静: 'i1', 开心: 'i2' }, defaultMood: '平静', currentMood: '开心', currentMoodUpdatedAt: '2026-10-28T12:00:00Z' });
  const message: Message = { id: 'm1', sequence: 1, sessionKey: 'role:r1', role: 'assistant', status: 'completed', createdAt: '2026-10-28T12:01:00Z', content: 'hi', metadata: { mood: '平静' } };
  expect(roleMood(owner).illustration?.id).toBe('i2');
  expect(roleMood(owner, [{ ...message, sessionKey: 'thread:other' }]).illustration?.id).toBe('i2');
  expect(roleMood(owner, [{ ...message, status: 'failed' }]).illustration?.id).toBe('i2');
  expect(roleMood(owner, [{ ...message, createdAt: '2026-10-28T11:00:00Z' }]).illustration?.id).toBe('i2');
  expect(roleMood(owner, [message]).illustration?.id).toBe('i1');
  expect(roleMood({ ...owner, illustrations: [calm] }).illustration?.id).toBe('i1');
});
