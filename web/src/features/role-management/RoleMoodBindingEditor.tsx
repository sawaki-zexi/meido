import { useState } from 'react';
import type { Role } from '../../api/types';

export function RoleMoodBindingEditor({ role, illustrationId, busy, onSave }: {
  role: Role; illustrationId: string; busy: boolean; onSave: (bindings: Record<string, string>) => void;
}) {
  const bindings = role.moodIllustrationBindings ?? {};
  const previous = Object.keys(bindings).find(mood => bindings[mood] === illustrationId) ?? '';
  const [mood, setMood] = useState(previous);
  const save = (value: string) => {
    // Shiori's one-to-one selection: moving a mood replaces its old picture.
    const next = Object.fromEntries(Object.entries(bindings).filter(([key, image]) => image !== illustrationId && key !== value));
    if (value) next[value] = illustrationId;
    onSave(next);
  };
  return <div className="illustration-toolbar">
    <label>绑定心情<input aria-label="绑定心情" maxLength={40} value={mood} disabled={busy} onChange={event => setMood(event.target.value)} placeholder="例如：平静、开心" /></label>
    <button type="button" disabled={busy || !mood.trim()} onClick={() => save(mood.trim())}>保存心情绑定</button>
    <button type="button" disabled={busy || !previous} onClick={() => save('')}>解除心情绑定</button>
    <small>每张插画绑定一种心情；同名心情将改用这张插画。不会因此允许角色发送图片。</small>
  </div>;
}
