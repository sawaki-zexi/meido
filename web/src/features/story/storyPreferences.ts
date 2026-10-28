/** Story-local presentation options; none alter the stored narrative. */
export type StoryPreferences = {
  textSpeed: 'slow' | 'normal' | 'fast';
  showFullText: boolean;
  autoPlay: boolean;
  autoPlayDelayMs: number;
  motionIntensity: 'reduced' | 'standard' | 'cinematic';
  reducedMotion: boolean;
};
const key = 'meido:story-preferences:v1';
export const defaultStoryPreferences: StoryPreferences = { textSpeed: 'normal', showFullText: false, autoPlay: false, autoPlayDelayMs: 1200, motionIntensity: 'standard', reducedMotion: false };
export function readStoryPreferences(): StoryPreferences {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? '{}') as Partial<StoryPreferences>;
    return { textSpeed: value.textSpeed === 'slow' || value.textSpeed === 'fast' ? value.textSpeed : 'normal', showFullText: value.showFullText === true, autoPlay: value.autoPlay === true, autoPlayDelayMs: typeof value.autoPlayDelayMs === 'number' && Number.isFinite(value.autoPlayDelayMs) ? Math.min(5000, Math.max(300, value.autoPlayDelayMs)) : 1200, motionIntensity: value.motionIntensity === 'reduced' || value.motionIntensity === 'cinematic' ? value.motionIntensity : 'standard', reducedMotion: value.reducedMotion === true };
  } catch { return defaultStoryPreferences; }
}
export function writeStoryPreferences(value: StoryPreferences) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Private browsing may disable persistence. */ }
}
