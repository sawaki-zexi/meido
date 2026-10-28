import type { StoryPreferences } from './storyPreferences';
import { Dialog } from '../../ui/Dialog';

export function StorySettings({ value, onChange, onClose, onOpenModelSettings, onOpenImageSettings }: { value: StoryPreferences; onChange: (value: StoryPreferences) => void; onClose: () => void; onOpenModelSettings: () => void; onOpenImageSettings: () => void }) {
  const update = <K extends keyof StoryPreferences>(key: K, next: StoryPreferences[K]) => onChange({ ...value, [key]: next });
  return <Dialog title="剧情设置" modalFocus onClose={onClose}>
    <div className="story-settings">
      <fieldset><legend>文字与播放</legend>
        <label className="field"><span>文字速度</span><select value={value.textSpeed} onChange={event => update('textSpeed', event.target.value as StoryPreferences['textSpeed'])}><option value="slow">慢</option><option value="normal">标准</option><option value="fast">快</option></select></label>
        <label className="checkbox-field"><input type="checkbox" checked={value.showFullText} onChange={event => update('showFullText', event.target.checked)} />立即显示全文</label>
        <label className="checkbox-field"><input type="checkbox" checked={value.autoPlay} onChange={event => update('autoPlay', event.target.checked)} />自动播放已生成剧情</label>
        <label className="field"><span>自动播放停留时间：{(value.autoPlayDelayMs / 1000).toFixed(1)} 秒</span><input type="range" min="300" max="5000" step="100" value={value.autoPlayDelayMs} onChange={event => update('autoPlayDelayMs', Number(event.target.value))} /></label>
      </fieldset>
      <fieldset><legend>演出</legend>
        <label className="field"><span>动效强度</span><select value={value.motionIntensity} onChange={event => update('motionIntensity', event.target.value as StoryPreferences['motionIntensity'])}><option value="reduced">轻量</option><option value="standard">标准</option><option value="cinematic">电影</option></select></label>
        <label className="checkbox-field"><input type="checkbox" checked={value.reducedMotion} onChange={event => update('reducedMotion', event.target.checked)} />减少动态效果</label>
      </fieldset>
      <div className="story-actions"><button type="button" onClick={onOpenModelSettings}>剧情模型连接</button><button type="button" onClick={onOpenImageSettings}>NovelAI 生图设置</button></div>
    </div>
  </Dialog>;
}
