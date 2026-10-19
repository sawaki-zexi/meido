export type AppearancePreferences = { fontSize: 'small' | 'normal' | 'large'; reducedMotion: boolean };
const key = 'meido:appearance';
export function readAppearance(): AppearancePreferences {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? '{}');
    return { fontSize: ['small', 'normal', 'large'].includes(value?.fontSize) ? value.fontSize : 'normal',
      reducedMotion: value?.reducedMotion === true };
  } catch { return { fontSize: 'normal', reducedMotion: false }; }
}
export function applyAppearance(value: AppearancePreferences) {
  document.documentElement.dataset.fontSize = value.fontSize;
  document.documentElement.dataset.reducedMotion = String(value.reducedMotion);
}
export function saveAppearance(value: AppearancePreferences) {
  applyAppearance(value);
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Current-window preferences still apply. */ }
}
