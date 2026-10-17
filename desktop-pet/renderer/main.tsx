// Adapted from Shiori-Agent 4f266ae, CodexSpritePetRenderer.tsx (MIT).
// Copyright (c) 2026 YinFengWindy. See third_party/shiori-agent/LICENSE.
import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { spriteAnimations, spriteFramePosition, spritePlaybackFrameAt, type SpriteState } from './spriteContract';
import './style.css';

declare global {
  interface Window { pet: { initial(): Promise<{ title: string }>; ready(): void; close(): void;
    receipt(id: string, status: 'started' | 'finished', frames: number): void;
    onAction(callback: (value: { actionId: string; state: string }) => void): () => void } }
}
function Pet() {
  const [title, setTitle] = useState('Meido 桌宠');
  const [action, setAction] = useState<{ actionId: string; state: SpriteState } | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const state = action?.state ?? 'idle';
  const frame = spritePlaybackFrameAt(state, playhead);
  useEffect(() => { void window.pet.initial().then((value) => setTitle(value.title));
    return window.pet.onAction((value) => { if (value.state in spriteAnimations) {
      setPlayhead(0); setAction({ ...value, state: value.state as SpriteState });
    } }); }, []);
  useEffect(() => {
    let timer = 0;
    const paint = requestAnimationFrame(() => {
      if (action && playhead === 0) window.pet.receipt(action.actionId, 'started', 1);
      const last = action && (state === 'idle' || playhead + 1 === spriteAnimations[state].frames * 3);
      timer = window.setTimeout(() => {
        if (last && action) { window.pet.receipt(action.actionId, 'finished', playhead + 1); setAction(null); setPlayhead(0); }
        else setPlayhead((value) => value + 1);
      }, state === 'idle' && action ? 32 : frame.duration);
    });
    return () => { cancelAnimationFrame(paint); clearTimeout(timer); };
  }, [action, state, playhead, frame.duration]);
  return <div title={title} aria-label="桌宠" className="pet" style={{ backgroundPosition: spriteFramePosition(frame.state, frame.frame) }}>
    <img hidden src="pet-asset://package/sprite" alt="" onLoad={() => requestAnimationFrame(() => requestAnimationFrame(() => window.pet.ready()))} />
    <button type="button" aria-label="关闭桌宠窗口" onClick={() => window.pet.close()}>×</button>
  </div>;
}
createRoot(document.getElementById('root')!).render(<Pet />);
