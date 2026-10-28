import { useRef, useState } from 'react';
import type { ChatAttachment } from '../../api/types';
import type { GenerationParameters } from '../../api/novelai';
import { Icon } from '../../ui/Icon';

type Props = { roleId: string; parameters: GenerationParameters; candidates: ChatAttachment[]; disabled: boolean;
  onPatch: (value: Partial<GenerationParameters>) => void; onRemove: () => void; onUpload: (file: File | undefined) => void };

export function StudioBaseImage({ roleId, parameters, candidates, disabled, onPatch, onRemove, onUpload }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [failedImage, setFailedImage] = useState('');
  const identifier = parameters.mode === 'img2img' ? parameters.base_attachment_id : undefined;
  return <section className="studio-base-field" aria-label="生图参考图设置">
    <div className="studio-field-label"><span>参考图</span><small>图生图 · 可选</small></div>
    <input ref={input} className="studio-file-input" aria-label="工作台上传生图底图" type="file" accept="image/png,image/jpeg,image/webp" disabled={disabled}
      onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) onUpload(file); }} />
    {identifier ? <div className="studio-base-card">
      <div className="studio-base-preview">
        {failedImage === identifier ? <p>参考图暂时无法预览，可更换图片。</p>
          : <img src={`/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(identifier)}`} alt="生图参考图" onError={() => setFailedImage(identifier)} />}
        <div className="studio-base-actions">
          <button type="button" aria-label="更换生图参考图" title="更换参考图" disabled={disabled} onClick={() => input.current?.click()}><Icon name="refresh" size={16} /></button>
          <button type="button" aria-label="移除生图参考图" title="移除参考图" disabled={disabled} onClick={onRemove}><Icon name="close" size={16} /></button>
        </div>
      </div>
      {(['strength', 'noise'] as const).map((key) => <label className="studio-slider" key={key}>
        <span>{key === 'strength' ? '重绘强度' : '噪声'}<output>{parameters[key] ?? (key === 'strength' ? .7 : .2)}</output></span>
        <input aria-label={key === 'strength' ? '工作台图生图强度' : '工作台图生图噪声'} type="range" min={key === 'strength' ? .01 : 0} max={key === 'strength' ? 1 : .99} step="0.01"
          disabled={disabled} value={parameters[key] ?? (key === 'strength' ? .7 : .2)} onChange={(event) => onPatch({ [key]: Number(event.target.value) })} />
      </label>)}
    </div> : <button type="button" className="studio-add-image" disabled={disabled} onClick={() => input.current?.click()}><Icon name="image" size={19} /><span>添加参考图</span><small>PNG / JPEG / WebP</small></button>}
    {(candidates.length > 0 || parameters.mode === 'img2img') && <label className="field studio-existing-image"><span>或选择角色已有图片</span>
      <select aria-label="工作台本角色底图" disabled={disabled} value={identifier ?? ''} onChange={(event) => {
        if (event.target.value) onPatch({ mode: 'img2img', base_attachment_id: event.target.value, strength: parameters.strength ?? .7, noise: parameters.noise ?? .2 });
        else onRemove();
      }}><option value="">选择底图</option>{candidates.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select>
    </label>}
  </section>;
}
