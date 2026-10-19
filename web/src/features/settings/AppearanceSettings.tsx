import { useState } from 'react';
import { readAppearance, saveAppearance, type AppearancePreferences } from './appearance';

export function AppearanceSettings() {
  const [value, setValue] = useState(readAppearance);
  function change(next: AppearancePreferences) { saveAppearance(next); setValue(next); }
  return <section className="settings-domain" aria-label="外观设置">
    <h2>外观</h2>
    <section className="settings-group" aria-label="显示"><h3>显示</h3><div className="settings-card">
      <label className="settings-preference"><span><strong>界面字号</strong><small>调整当前浏览器的文字大小。</small></span>
        <select aria-label="界面字号" value={value.fontSize} onChange={event => change({ ...value, fontSize: event.target.value as AppearancePreferences['fontSize'] })}>
          <option value="small">较小</option><option value="normal">标准</option><option value="large">较大</option>
        </select>
      </label>
    </div></section>
    <section className="settings-group" aria-label="动效"><h3>动效</h3><div className="settings-card">
      <div className="settings-preference"><span><strong>减弱动态效果</strong><small>减少过渡和动画；系统的减弱动态设置也会生效。</small></span>
        <button className="settings-switch" type="button" role="switch" aria-label="减弱动态效果" aria-checked={value.reducedMotion}
          onClick={() => change({ ...value, reducedMotion: !value.reducedMotion })}><span /></button>
      </div>
    </div></section>
    <p className="field-hint">这些偏好立即生效，仅保存在当前浏览器。</p>
  </section>;
}
