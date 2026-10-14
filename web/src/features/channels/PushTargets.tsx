import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";

type PushTarget = {
  accountId: string;
  targetId: string;
  displayName: string;
  enabled: boolean;
  available: boolean;
};

export function PushTargets({ roleId, accountId }: { roleId: string; accountId?: string }) {
  const [targets, setTargets] = useState<PushTarget[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const sequence = ++request.current;
    try {
      const result = await api<{ targets: PushTarget[] }>(`/api/roles/${encodeURIComponent(roleId)}/channels/push-targets`);
      if (sequence === request.current) {
        setTargets(result.targets.filter((target) => target.accountId === accountId));
        setError("");
      }
    } catch (failure) {
      if (sequence === request.current) setError(errorMessage(failure, "读取提醒投递目标失败"));
    }
  }, [roleId, accountId]);
  useEffect(() => {
    setBusy(false);
    setError("");
    if (accountId) void refresh();
    else setTargets([]);
    return () => { request.current += 1; };
  }, [accountId, refresh]);

  async function select(target: PushTarget, enabled: boolean) {
    const sequence = ++request.current;
    setBusy(true);
    try {
      const result = await api<{ targets: PushTarget[] }>(`/api/roles/${encodeURIComponent(roleId)}/channels/accounts/${encodeURIComponent(target.accountId)}/push-target`, {
        method: "PUT", body: JSON.stringify({ targetId: enabled ? target.targetId : null }),
      });
      if (sequence === request.current) {
        setTargets(result.targets.filter((item) => item.accountId === accountId));
        setError("");
      }
    } catch (failure) {
      if (sequence === request.current) setError(errorMessage(failure, "更新提醒投递目标失败"));
    } finally {
      if (sequence === request.current) setBusy(false);
    }
  }

  return <section className="channel-push-targets" aria-label="主动提醒投递">
    <div className="channel-identities-heading"><h4>主动提醒投递</h4><button type="button" disabled={busy || !accountId} onClick={() => void refresh()}>刷新目标</button></div>
    <p>选择已配对主人私聊接收此角色的待办提醒；桌面提醒仍会保存。</p>
    {error && <p role="status" className="channel-error">{error}</p>}
    {targets.length === 0 && <p className="channel-empty">请先在此账号私聊配对，再刷新目标。</p>}
    {targets.map((target) => <label key={target.targetId}>
      <input type="checkbox" checked={target.enabled} disabled={busy} onChange={(event) => void select(target, event.target.checked)} />
      {target.displayName}{target.available ? "" : "（账号已停用）"}
    </label>)}
  </section>;
}
