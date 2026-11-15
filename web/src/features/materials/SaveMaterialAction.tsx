import { useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { materials } from '../../api/materials';
import { Dialog } from '../../ui/Dialog';
import './materials.css';

export function SaveMaterialAction({ roleId, messageId, attachmentId }: { roleId: string; messageId: string; attachmentId: string }) {
  const [open, setOpen] = useState(false);
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [error, setError] = useState('');
  const submitting = useRef(false);
  return <div className="material-message-action">
    <button type="button" className="ghost" onClick={() => { setOpen(true); setError(''); }}>保存为表情</button>
    {feedback && <span role="status">{feedback}</span>}
    {open && <Dialog title="保存为表情" modalFocus onClose={() => { if (!busy) setOpen(false); }}>
      <form className="material-import" onSubmit={async event => {
        event.preventDefault();
        if (submitting.current) return;
        submitting.current = true; setBusy(true); setError('');
        try {
          const result = await materials.saveMessage(roleId, messageId, attachmentId, description.trim());
          setFeedback(result.deduplicated ? '已在素材库中，已保留这次来源。' : '已保存到素材库。');
          setOpen(false); setDescription('');
        } catch (cause) {
          setError(errorMessage(cause, '保存尚未确认，请到素材库核对后再操作。'));
        } finally { submitting.current = false; setBusy(false); }
      }}>
        <p>保存原图。说明可以以后补充；留空时不会自动推荐。渠道图片保留原会话的使用范围。</p>
        <label className="field"><span>说明（可选）</span><textarea aria-label="说明（可选）" rows={3} maxLength={2000} value={description} onChange={event => setDescription(event.target.value)} disabled={busy} /></label>
        {error && <p role="alert">{error}</p>}
        <div className="material-dialog-actions"><button type="button" className="ghost" onClick={() => setOpen(false)} disabled={busy}>取消</button><button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '确认保存'}</button></div>
      </form>
    </Dialog>}
  </div>;
}
