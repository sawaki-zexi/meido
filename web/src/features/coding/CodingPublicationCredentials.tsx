import { useEffect, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import { getCodingPublicationSettings, setCodingPublicationCredential, type CodingPublicationSettings } from "../../api/coding";
import { Notice, Placeholder } from "../../ui/Status";

function usePublicationCredential() {
  const [token, setToken] = useState("");
  const [settings, setSettings] = useState<CodingPublicationSettings | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const live = useRef(true), pending = useRef(false);
  const generation = useRef(0);
  useEffect(() => { live.current = true; const version = generation.current; void getCodingPublicationSettings().then(value => { if (live.current && generation.current === version) setSettings(value); }).catch(cause => { if (live.current && generation.current === version) setError(errorMessage(cause, "发布凭据状态不可读")); }); return () => { live.current = false; }; }, []);
  const save = async (value = token) => {
    if (pending.current) return false;
    generation.current += 1;
    pending.current = true; setBusy(true); setError("");
    try { const setting = await setCodingPublicationCredential(value); if (live.current) { setSettings(setting); setToken(""); } return true; }
    catch (cause) { if (live.current) setError(errorMessage(cause, "发布凭据未确认保存")); return false; }
    finally { pending.current = false; if (live.current) setBusy(false); }
  };
  return { settings, busy, error, token, setToken, save, dismiss: () => setError("") };
}

export function CodingPublicationCredentials() {
  const state = usePublicationCredential();
  return <form onSubmit={event => { event.preventDefault(); void state.save(); }}>
    <fieldset disabled={state.busy}><legend>独立发布凭据</legend>
      <p>仅供主人批准具体 HEAD 后的宿主发布操作使用，不交给原生编程 agent，也不会读取本机 GitHub 登录。需要测试或真实发布时，由主人明确提供对应仓库的权限。</p>
      {state.error && <Notice onDismiss={state.dismiss}>{state.error}</Notice>}
      {!state.settings ? <Placeholder loading={!state.error}>{state.error ? "发布凭据状态未确认" : "正在读取发布凭据状态…"}</Placeholder> : <p>{state.settings.cleanupPending ? "旧凭据清理未确认，发布保持关闭" : state.settings.configured ? "已配置独立发布凭据" : "尚未配置发布凭据"}</p>}
      <label className="field">独立发布凭据<input type="password" aria-label="独立发布凭据" autoComplete="new-password" maxLength={4096} value={state.token} onChange={event => state.setToken(event.target.value)} /></label>
      <button type="submit" disabled={!state.token}>保存发布凭据</button>
      <button type="button" disabled={!state.settings?.configured && !state.settings?.cleanupPending} onClick={() => void state.save("")}>移除发布凭据</button>
    </fieldset>
  </form>;
}
