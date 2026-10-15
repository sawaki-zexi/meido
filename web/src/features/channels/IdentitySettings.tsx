import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { IdentityBindings } from "./IdentityBindings";
import "./channel-settings.css";

type Pairing = { code: string; expiresAt: string };

export function IdentitySettings() {
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [now, setNow] = useState(Date.now);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    if (!pairing) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [pairing]);
  const seconds = pairing ? Math.max(0, Math.ceil((Date.parse(pairing.expiresAt) - now) / 1000)) : 0;
  const code = seconds > 0 ? pairing?.code : null;

  const generate = async () => {
    setBusy(true); setFeedback("");
    try {
      const result = await api<Pairing>("/api/channels/pairing", { method: "POST" });
      if (!Number.isFinite(Date.parse(result.expiresAt)) || Date.parse(result.expiresAt) <= Date.now()) throw new Error("配对码已过期，请重新生成。");
      if (active.current) { setNow(Date.now()); setPairing(result); }
    } catch (cause) { if (active.current) setFeedback(errorMessage(cause, "生成配对码失败，请重试")); }
    finally { if (active.current) setBusy(false); }
  };
  const copy = async () => {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      if (active.current) setFeedback("配对码已复制");
    } catch { if (active.current) setFeedback("无法自动复制，请选中配对码手动复制。"); }
  };
  return <section className="channel-settings" aria-label="我的身份">
    <header><h3>我的身份</h3><p>让角色在其他平台认出你。先连接角色账号，再用自己的平台账号私聊发送配对码。</p></header>
    <div className="channel-setting-card">
      <h4>主人配对</h4>
      {code && <div className="pairing-code-row"><output aria-label="配对码">{code}</output><span role="timer" aria-label="配对码剩余时间">{String(Math.floor(seconds / 60)).padStart(2, "0")}:{String(seconds % 60).padStart(2, "0")}</span><button type="button" onClick={() => void copy()}>复制配对码</button></div>}
      {pairing && !code && <p role="status">配对码已过期，请重新生成。</p>}
      <button type="button" className="primary" disabled={busy} onClick={() => void generate()}>{busy ? "生成中…" : pairing ? "重新生成配对码" : "生成配对码"}</button>
      <p>10 分钟内有效，只能使用一次。可发给任一已连接的角色账号；重新生成后旧码失效。</p>
      {feedback && <p role="status">{feedback}</p>}
    </div>
    <IdentityBindings watchPairing={Boolean(code)} onPaired={() => { setPairing(null); setFeedback("配对成功，已识别你的平台身份。"); }} />
  </section>;
}
