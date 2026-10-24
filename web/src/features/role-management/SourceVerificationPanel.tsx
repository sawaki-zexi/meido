import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { SourceArtifactPreview } from "./SourceArtifactPreview";

export type SourceVerification = {
  id: string; sourceId: string; sourceRevision: number; status: string;
  proof: { proofId: string; sourceId: string; sourceRevision: number; verifierVersion?: string; collectionVerified?: boolean; ackVerified?: boolean } | null;
  runtimeAcknowledged: boolean; cleanupPending: boolean; error: string;
};

export function SourceVerificationPanel({ roleId, revision, sources, attempts, refresh, importProof, importSources }: {
  roleId: string; revision: number; sources: Record<string, unknown>[]; attempts: SourceVerification[];
  refresh: () => Promise<void>; importProof: (proof: NonNullable<SourceVerification["proof"]>) => boolean;
  importSources: (sources: Record<string, unknown>[]) => boolean;
}) {
  const [sourceId, setSourceId] = useState("");
  const [manifestPath, setManifestPath] = useState("");
  const [manifestSha256, setManifestSha256] = useState("");
  const [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const pending = useRef<{ fingerprint: string; nonce: string; acceptedId?: string } | null>(null);
  const mounted = useRef(true);
  const path = `/api/roles/${encodeURIComponent(roleId)}/autonomy/source-verifications`;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (pending.current?.acceptedId && attempts.some((attempt) => attempt.id === pending.current?.acceptedId
      && attempt.runtimeAcknowledged && !attempt.cleanupPending)) pending.current = null;
    if (!attempts.some((attempt) => !attempt.runtimeAcknowledged || attempt.cleanupPending)) return;
    const timer = window.setTimeout(() => { void refresh(); }, 1000);
    return () => window.clearTimeout(timer);
  }, [attempts, refresh]);

  async function verify() {
    if (busy || !trusted) return;
    const fingerprint = JSON.stringify([revision, sourceId, manifestPath, manifestSha256]);
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, nonce: crypto.randomUUID() };
    setBusy(true); setError(""); setMessage("");
    try {
      const accepted = await api<{ id: string }>(path, { method: "POST", body: JSON.stringify({ requestNonce: pending.current.nonce,
        expectedRevision: revision, sourceId, manifestPath, manifestSha256, trustLocalCode: true }) });
      if (!mounted.current) return;
      pending.current.acceptedId = accepted.id;
      setTrusted(false);
      await refresh();
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause, "来源验证暂不可提交，重试会查询原请求"));
    } finally { if (mounted.current) setBusy(false); }
  }

  async function cancel(id: string) {
    setBusy(true); setError("");
    try {
      await api(`${path}/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      if (mounted.current) await refresh();
    } catch (cause) {
      if (mounted.current) setError(errorMessage(cause, "验证取消暂不可确认"));
    } finally { if (mounted.current) setBusy(false); }
  }

  const available = sources.filter((source) => source.enabled && (source.ackContract === "absolute-idempotent"
    || source.channel === "context" && source.ackContract === "none"));
  const contextOnly = available.some((source) => source.sourceId === sourceId && source.channel === "context");
  const ready = trusted && available.some((source) => source.sourceId === sourceId)
    && manifestPath.trim().length > 0 && /^[a-f0-9]{64}$/.test(manifestSha256);
  return <fieldset aria-label="来源 ACK 契约验证" disabled={busy}>
    <legend className="settings-sr-only">来源 ACK 契约验证</legend>
    <SourceArtifactPreview key={roleId} roleId={roleId} importSources={importSources} importManifest={(path, sha) => {
      setManifestPath(path); setManifestSha256(sha); setTrusted(false); setMessage("");
    }} />
    <div className="settings-subsection">
    <h4>契约验证</h4>
    <p className="field-hint">先保存来源许可。验证会运行本地源码，使用临时数据，不使用服务账号凭据。</p>
    <label className="field"><span>待验证来源</span><select aria-label="待验证来源" value={sourceId}
      onChange={(event) => { setSourceId(event.target.value); setTrusted(false); }}>
      <option value="">请选择已保存的来源</option>
      {available.map((source) => <option key={String(source.sourceId)} value={String(source.sourceId)}>{String(source.sourceId)}</option>)}
    </select></label>
    <div className="settings-field-grid">
    <label className="field"><span>来源验证清单路径</span><input aria-label="来源验证清单路径" value={manifestPath}
      onChange={(event) => { setManifestPath(event.target.value); setTrusted(false); }} /></label>
    <label className="field"><span>来源验证清单 SHA256</span><input aria-label="来源验证清单 SHA256" value={manifestSha256}
      onChange={(event) => { setManifestSha256(event.target.value.trim().toLowerCase()); setTrusted(false); }} /></label>
    </div>
    <label className="field checkbox-field"><input type="checkbox" checked={trusted}
      onChange={(event) => setTrusted(event.target.checked)} /><span>信任并运行该本地来源产物</span></label>
    <button type="button" disabled={!ready} onClick={() => void verify()}>{contextOnly ? "验证来源读取契约" : "验证来源 ACK 契约"}</button>
    {error && <p role="status">{error}</p>}{message && <p role="status">{message}</p>}
    <div aria-label="来源验证结果">{attempts.map((attempt) => <div key={attempt.id}>
      <p>{attempt.sourceId}：{attempt.cleanupPending ? "正在结算验证资源" : attempt.status === "succeeded" && attempt.proof ? "契约验证通过" : attempt.status === "cancelled" ? "已取消" : attempt.status === "failed" || attempt.status === "rejected" ? "验证未通过" : "验证处理中"}</p>
      {attempt.error && <p>{attempt.error}</p>}
      {attempt.proof?.collectionVerified && <p>已验证该通道的稳定数据、正文关联、空值与读取失败区别。</p>}
      {attempt.proof?.ackVerified === false && <p>仅验证读取契约；context 不需要 ACK，也不会取得 ACK 重试资格。</p>}
      {attempt.proof && attempt.proof.ackVerified !== false && attempt.runtimeAcknowledged && !attempt.cleanupPending && <button type="button"
        aria-label={`使用 ${attempt.sourceId} 的验证结果`} onClick={() => {
          if (importProof(attempt.proof!)) setMessage("验证引用已填入草稿，请保存自主设置后启用。");
        }}>使用验证结果</button>}
      {!attempt.runtimeAcknowledged && <button type="button" onClick={() => void cancel(attempt.id)}>取消 {attempt.sourceId} 验证</button>}
    </div>)}</div>
    </div>
  </fieldset>;
}
