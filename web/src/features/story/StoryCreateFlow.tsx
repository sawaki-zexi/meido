import { useState } from 'react';
import type { Role } from '../../api/types';
import type { StoryCapabilities } from '../../api/story';
import { Avatar } from '../../ui/Avatar';
import { Notice } from '../../ui/Status';
import type { StoryCreationInput } from './types';
import { creationSteps, isCreationStepComplete } from './storyCreationWizard';
import { STORY_TIME_BANDS, type StoryTimeBand } from './storyTime';

const labels = ['选择角色', '故事设定', '玩家身份', '确认开场'];

export function StoryCreateFlow({ roles, value, onChange, onSubmit, onBack, busy, capabilities }: { roles: Role[]; value: StoryCreationInput; onChange: (value: StoryCreationInput) => void; onSubmit: () => void; onBack: () => void; busy: boolean; capabilities: StoryCapabilities | null }) {
  const [step, setStep] = useState(0);
  const role = roles.find(item => item.id === value.roleId);
  const patch = (change: Partial<StoryCreationInput>) => onChange({ ...value, ...change });
  const player = (change: Partial<StoryCreationInput['playerProfile']>) => patch({ playerProfile: { ...value.playerProfile, ...change } });
  return <section className="story-create" aria-label="新建剧情">
    <header className="story-page-header"><button type="button" onClick={onBack}>返回主菜单</button><h1>新建剧情</h1></header>
    <ol className="story-steps" aria-label="创建步骤">{labels.map((label, index) => <li key={label} aria-current={step === index ? 'step' : undefined}><button type="button" disabled={busy || index > step} onClick={() => setStep(index)}>{index + 1}. {label}</button></li>)}</ol>
    <form className="story-create-form" onSubmit={event => { event.preventDefault(); if (!isCreationStepComplete(creationSteps[step], value) || busy) return; if (step < 3) setStep(step + 1); else onSubmit(); }}>
      <fieldset disabled={busy}>
      {step === 0 && <><legend>选择一位角色</legend><div className="story-role-grid">{roles.map(item => <button key={item.id} type="button" className="story-role-choice" aria-pressed={value.roleId === item.id} onClick={() => patch({ roleId: item.id })}><Avatar id={item.id} name={item.name} avatarUrl={item.avatarUrl} /><span><strong>{item.name}</strong><small>{item.description}</small></span></button>)}</div>{!roles.length && <Notice>请先在角色页创建角色。</Notice>}</>}
      {step === 1 && <><legend>故事设定</legend><label className="field"><span>剧情标题</span><input autoFocus required maxLength={200} value={value.title} onChange={event => patch({ title: event.target.value })} /></label><label className="field"><span>世界背景与开场方向</span><textarea required maxLength={12000} rows={6} value={value.background} onChange={event => patch({ background: event.target.value })} placeholder="地点、与角色的关系，以及希望发生的故事…" /></label><div className="story-two-fields"><label className="field"><span>故事日期</span><input required type="date" value={value.storyDate} onChange={event => patch({ storyDate: event.target.value })} /></label><label className="field"><span>开场时段</span><select required value={value.timeBand} onChange={event => patch({ timeBand: event.target.value as StoryTimeBand })}><option value="">请选择</option>{STORY_TIME_BANDS.map(band => <option key={band}>{band}</option>)}</select></label></div></>}
      {step === 2 && <><legend>玩家身份</legend><label className="field"><span>称呼</span><input autoFocus required maxLength={200} value={value.playerProfile.displayName} onChange={event => player({ displayName: event.target.value })} /></label><label className="field"><span>身份与角色关系</span><textarea required maxLength={4000} rows={3} value={value.playerProfile.identity} onChange={event => player({ identity: event.target.value })} /></label><label className="field"><span>外观</span><textarea required maxLength={4000} rows={3} value={value.playerProfile.appearance} onChange={event => player({ appearance: event.target.value })} placeholder="用于剧情与人物 CG 的外观描述" /></label></>}
      {step === 3 && <><legend>确认开场</legend><div className="story-review"><h2>{value.title}</h2><p>{role?.name} · {value.storyDate} · {value.timeBand}</p><p className="story-preserve">{value.background}</p><dl><dt>玩家</dt><dd>{value.playerProfile.displayName}</dd><dt>身份</dt><dd>{value.playerProfile.identity}</dd><dt>外观</dt><dd>{value.playerProfile.appearance}</dd></dl></div><p className="muted">开始后会使用所选角色的模型连接生成剧情；角色设定与玩家身份会保存到这个故事。</p><label className="checkbox-field"><input type="checkbox" checked={value.generateImages !== false} onChange={event => patch({ generateImages: event.target.checked, novelaiRevision: capabilities?.novelaiRevision })} />生成背景与 CG</label><p className="field-hint">使用现有 NovelAI 设置与有限额度，每次开场或行动最多 1 张图片，可能产生费用。关闭可游玩文字剧情。</p>{!capabilities?.imageEnabled && <p className="field-hint">{capabilities?.imageReason || '正在读取 NovelAI 设置…'}</p>}</>}
      </fieldset>
      <footer className="story-actions"><button type="button" disabled={busy || step === 0} onClick={() => setStep(step - 1)}>上一步</button><button type="submit" disabled={busy || !isCreationStepComplete(creationSteps[step], value) || (step === 3 && !role)}>{busy ? '正在创建…' : step < 3 ? '下一步' : '开始故事'}</button></footer>
    </form>
  </section>;
}
