import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";

type Identity = {
  identityId: string;
  channel: string;
  scope: "platform" | "account";
  platformUserId: string;
  accountId: string | null;
  pairedAt: string;
};

export function IdentityBindings({ channel, accountId }: { channel: string; accountId?: string }) {
  const [identities, setIdentities] = useState<Identity[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const sequence = ++request.current;
    try {
      const result = await api<{ identities: Identity[] }>("/api/channels/identities");
      if (sequence === request.current) {
        setIdentities(result.identities.filter((item) => item.channel === channel && (item.scope === "platform" || item.accountId === accountId)));
        setError("");
      }
    } catch (failure) {
      if (sequence === request.current) setError(errorMessage(failure, "无法读取主人绑定"));
    }
  }, [channel, accountId]);
  useEffect(() => {
    void refresh();
    return () => { request.current += 1; };
  }, [refresh]);

  async function unbind(identity: Identity) {
    setBusy(identity.identityId);
    try {
      await api(`/api/channels/identities/${encodeURIComponent(identity.identityId)}`, { method: "DELETE" });
      setIdentities((items) => items.filter((item) => item.identityId !== identity.identityId));
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "解绑失败，请重试"));
    } finally {
      setBusy(null);
    }
  }

  return <div className="channel-identities">
    <div className="channel-identities-heading"><h4>已配对主人</h4><button type="button" onClick={() => void refresh()}>刷新绑定</button></div>
    {error && <p role="status" className="channel-error">{error}</p>}
    {identities.length === 0 && <p className="channel-empty">暂无已配对主人</p>}
    {identities.map((identity) => <div className="channel-identity" key={identity.identityId}>
      <div><strong>{identity.platformUserId}</strong><small>{identity.scope === "platform" ? "此平台所有角色账号均识别为主人" : "仅此应用账号识别为主人"}</small></div>
      <button type="button" aria-label={`解绑主人 ${identity.platformUserId}`} disabled={busy !== null} onClick={() => void unbind(identity)}>{busy === identity.identityId ? "解绑中…" : "解绑"}</button>
    </div>)}
    {identities.length > 0 && <p>解绑后立即取消主人权限、私聊上下文共享和主人投递目标；聊天记录保留。</p>}
  </div>;
}
