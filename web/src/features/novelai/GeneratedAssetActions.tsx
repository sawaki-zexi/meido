import { useEffect, useRef, useState } from 'react';
import { roleAssets } from '../../api/roleAssets';
import { errorMessage } from '../../api/client';
import type { Role } from '../../api/types';

/** Applying an owned history image is local only: it never regenerates or sends. */
export function GeneratedAssetActions({ roleId, attachmentId, disabled = false, onApplied }: {
  roleId: string; attachmentId: string; disabled?: boolean; onApplied?: (role: Role) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const pending = useRef(false);
  const scope = useRef(0);
  useEffect(() => { scope.current++; setMessage(''); setBusy(false); pending.current = false; return () => { scope.current++; }; }, [roleId, attachmentId]);
  const apply = async (background: boolean) => {
    if (pending.current || disabled) return;
    const version = scope.current;
    pending.current = true; setBusy(true); setMessage('');
    try {
      const { role } = await roleAssets.importAttachment(roleId, attachmentId, background);
      if (scope.current !== version) return;
      onApplied?.(role);
      setMessage(background ? '已保存为角色插画并设为聊天背景。' : '已保存到角色插画库。');
    } catch (cause) { if (scope.current === version) setMessage(errorMessage(cause, '插画操作未确认，请查看角色素材库')); }
    finally { if (scope.current === version) { pending.current = false; setBusy(false); } }
  };
  return <div className="generated-asset-actions"><button type="button" disabled={busy || disabled} onClick={() => void apply(false)}>保存为角色插画</button>
    <button type="button" disabled={busy || disabled} onClick={() => void apply(true)}>设为聊天背景</button>
    {message && <p role="status">{message}</p>}</div>;
}
