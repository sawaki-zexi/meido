import { useEffect, useState } from 'react';
import { readAppearance, saveAppearance, type AppearancePreferences } from './appearance';
import { SettingsSwitch } from './SettingsSwitch';

export function AppearanceSettings() {
  const [value, setValue] = useState(readAppearance);
  const [systemReducedMotion, setSystemReducedMotion] = useState(() => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false);
  useEffect(() => {
    const query = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    if (!query) return;
    const changed = () => setSystemReducedMotion(query.matches);
    query.addEventListener('change', changed);
    return () => query.removeEventListener('change', changed);
  }, []);
  const motionDisabled = value.reducedMotion || systemReducedMotion;
  function change(next: AppearancePreferences) { saveAppearance(next); setValue(next); }
  return <section className="settings-domain" aria-label="外观设置">
    <h2>外观</h2>
    <section className="settings-group" aria-label="聊天"><h3>聊天</h3><div className="settings-card">
      <div className="settings-preference"><span><strong>实时显示回复</strong><small>生成时逐步显示文字；关闭后在完成时显示回复。</small></span>
        <SettingsSwitch label="实时显示回复" checked={value.streamReplies} onChange={streamReplies => change({ ...value, streamReplies })} />
      </div>
    </div></section>
    <section className="settings-group" aria-label="显示"><h3>显示</h3><div className="settings-card">
      <label className="settings-preference"><span><strong>界面字号</strong><small>调整当前浏览器的文字大小。</small></span>
        <select aria-label="界面字号" value={value.fontSize} onChange={event => change({ ...value, fontSize: event.target.value as AppearancePreferences['fontSize'] })}>
          <option value="small">较小</option><option value="normal">标准</option><option value="large">较大</option>
        </select>
      </label>
    </div></section>
    <section className="settings-group" aria-label="动效"><h3>动效</h3><div className="settings-card">
      <div className="settings-preference"><span><strong>背景呼吸</strong><small>{systemReducedMotion ? '系统已开启减弱动态效果。' : value.reducedMotion ? '减弱动态效果已开启，背景动效暂停。' : '让应用背景缓慢变化。'}</small></span>
        <SettingsSwitch label="背景呼吸" checked={value.backdropMotion && !motionDisabled} disabled={motionDisabled} onChange={backdropMotion => change({ ...value, backdropMotion })} />
      </div>
      <div className="settings-preference"><span><strong>减弱动态效果</strong><small>减少过渡和动画；系统的减弱动态设置也会生效。</small></span>
        <SettingsSwitch label="减弱动态效果" checked={value.reducedMotion} onChange={reducedMotion => change({ ...value, reducedMotion })} />
      </div>
    </div></section>
    <p className="field-hint">这些偏好立即生效，仅保存在当前浏览器。</p>
  </section>;
}
