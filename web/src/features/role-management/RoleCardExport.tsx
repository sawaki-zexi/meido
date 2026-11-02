import { useEffect, useId, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { exportRoleCard, type CardExportFormat, type CardExportSummary } from '../../api/roleExport';
import './roleCardExport.css';

type Props = { roleId: string; revision: string; blockedReason: string };
const descriptions = {
  json: '只包含角色设定，不包含图片。',
  png: '设定与单张封面。优先使用卡片图，其次头像；无图时使用透明占位。',
  charx: '包含角色设定、当前头像和卡片图，适合迁移到支持 CHARX 的客户端。',
};

export function RoleCardExport({ roleId, revision, blockedReason }: Props) {
  const [open, setOpen] = useState(false);
  const [format, setFormat] = useState<CardExportFormat>('json');
  const [pending, setPending] = useState(false);
  const [summary, setSummary] = useState<CardExportSummary | null>(null);
  const [error, setError] = useState('');
  const panelId = useId();
  const context = JSON.stringify([roleId, revision, blockedReason]);
  const latest = useRef(context);
  latest.current = context;
  const request = useRef<AbortController | null>(null);
  const urls = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    setPending(false); setSummary(null); setError('');
    return () => { request.current?.abort(); };
  }, [context]);
  useEffect(() => () => {
    for (const [url, timer] of urls.current) { clearTimeout(timer); URL.revokeObjectURL(url); }
    urls.current.clear();
  }, []);

  async function download() {
    if (blockedReason || pending) return;
    const controller = new AbortController();
    request.current = controller;
    const current = () => !controller.signal.aborted && latest.current === context;
    setPending(true); setSummary(null); setError('');
    try {
      const artifact = await exportRoleCard(roleId, format, controller.signal);
      if (!current()) return;
      const url = URL.createObjectURL(artifact.blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = artifact.filename;
      document.body.appendChild(anchor);
      try { anchor.click(); } finally {
        anchor.remove();
        urls.current.set(url, setTimeout(() => { URL.revokeObjectURL(url); urls.current.delete(url); }, 0));
      }
      setSummary(artifact.summary);
    } catch (cause) {
      if (current()) setError(errorMessage(cause, '导出失败，请重试。'));
    } finally {
      if (current()) setPending(false);
    }
  }

  return <section className="role-card-export" aria-label="角色卡导出">
    <button type="button" className="ghost" aria-expanded={open} aria-controls={panelId} onClick={() => setOpen(value => !value)}>导出角色卡</button>
    {open && <div id={panelId} className="role-card-export-panel">
      <div className="role-card-export-controls">
        <label>导出格式<select value={format} disabled={pending} onChange={event => { setFormat(event.target.value as CardExportFormat); setSummary(null); setError(''); }}>
          <option value="json">JSON · 仅设定</option><option value="png">PNG · 图片角色卡</option><option value="charx">CHARX · 设定与图片包</option>
        </select></label>
        <button type="button" className="primary" disabled={Boolean(blockedReason) || pending} onClick={() => void download()}>{pending ? '导出中…' : '下载角色卡'}</button>
      </div>
      <p className="muted">{descriptions[format]}</p>
      <p className="muted">仅导出已保存资料，不含模型连接、权限、会话或记忆。</p>
      {blockedReason && <p>{blockedReason}</p>}
      {error && <p role="alert" className="role-card-export-error">{error}</p>}
      {summary && <div role="status" className="role-card-export-summary">
        <p><strong>{summary.format.toUpperCase()} 已下载</strong> · 包含{summary.fields.join('、')}</p>
        {summary.includedAssets.length > 0 && <p>包含图片：{summary.includedAssets.join('、')}</p>}
        {summary.omittedAssets.length > 0 && <p>未包含图片：{summary.omittedAssets.join('、')}</p>}
        {summary.notes.map((note, index) => <p key={index}>{note}</p>)}
      </div>}
    </div>}
  </section>;
}
