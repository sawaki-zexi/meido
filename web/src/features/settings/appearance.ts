export type AppearancePreferences = { fontSize: 'small' | 'normal' | 'large'; reducedMotion: boolean; streamReplies: boolean; backdropMotion: boolean };
const key = 'meido:appearance';
export function readAppearance(): AppearancePreferences {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? '{}');
    return { fontSize: ['small', 'normal', 'large'].includes(value?.fontSize) ? value.fontSize : 'normal',
      reducedMotion: value?.reducedMotion === true, streamReplies: value?.streamReplies !== false, backdropMotion: value?.backdropMotion !== false };
  } catch { return { fontSize: 'normal', reducedMotion: false, streamReplies: true, backdropMotion: true }; }
}
export function applyAppearance(value: AppearancePreferences) {
  document.documentElement.dataset.fontSize = value.fontSize;
  document.documentElement.dataset.reducedMotion = String(value.reducedMotion);
  document.documentElement.dataset.streamReplies = String(value.streamReplies);
  document.documentElement.dataset.backdropMotion = String(value.backdropMotion);
}
export function saveAppearance(value: AppearancePreferences) {
  applyAppearance(value);
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Current-window preferences still apply. */ }
}
