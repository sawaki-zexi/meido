import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage } from "../../api/client";
import { channelName } from "./channelAccounts";

type Versioned = { revision: number; readOnly: boolean };
type Candidate = { id: string; threadId: string; profile: string; shorthand: string; updatedAt: string };
type Member = Versioned & { key: string; channel: string; scope: string; senderId: string; name: string; nicknames: string[];
  profile: string; shorthand: string; state: string; sources: { threadId: string; title: string; lastSeen: string }[]; candidates: Candidate[] };
type Conversation = Versioned & { note: string; activity: { text: string; updatedAt: string }; members: Member[] };
type Props = { roleId: string; threadId: string; title: string; refresh?: number; onBack: () => void };

function useDocument<T extends Versioned>(url: string, refresh: number, apply: (value: T) => void) {
  const [value, setValue] = useState<T | null>(null);
  const [remote, setRemote] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);
  const [busy, setBusy] = useState(false);
  const dirty = useRef(false), live = useRef(true), request = useRef(0), current = useRef<T | null>(null);
  const applyRef = useRef(apply); applyRef.current = apply;
  const fail = (cause: unknown) => {
    if (!live.current) return;
    setDenied(cause instanceof ApiError && [403, 404].includes(cause.status));
    setError(cause instanceof ApiError && cause.status === 409 ? "资料已更新，草稿已保留。请重新读取并合并后保存。" : errorMessage(cause, "无法读取资料"));
  };
  const load = useCallback(async (merge = false) => {
    const sequence = ++request.current;
    try {
      const result = await api<T>(url);
      if (!live.current || sequence !== request.current) return;
      setDenied(false);
      if (dirty.current && current.current && result.revision !== current.current.revision && !merge) {
        setRemote(result); setError("资料已更新，草稿已保留。请重新读取并合并后保存。"); return;
      }
      setValue(result); current.current = result; setRemote(dirty.current ? result : null); setError("");
      if (!dirty.current) applyRef.current(result);
    } catch (cause) { if (sequence === request.current) fail(cause); }
  }, [url]);
  useEffect(() => { live.current = true; void load(); return () => { live.current = false; request.current += 1; }; }, [load, refresh]);
  const save = async (path: string, body: object, method = "PUT") => {
    if (!current.current || busy) return false;
    setBusy(true); setError("");
    try {
      const result = await api<T>(path, { method, body: method === "DELETE" ? undefined : JSON.stringify({ ...body, expectedRevision: current.current.revision }) });
      if (!live.current) return false;
      request.current += 1; dirty.current = false; setRemote(null);
      if (method !== "DELETE") { setValue(result); current.current = result; applyRef.current(result); }
      return true;
    } catch (cause) { fail(cause); return false; }
    finally { if (live.current) setBusy(false); }
  };
  return { value, remote, error, denied, busy, dirty, load, save, restore: () => {
    if (current.current) applyRef.current(current.current);
    dirty.current = false; setRemote(null); void load();
  } };
}

export function ExternalMemoryPanel(props: Props) {
  return <ConversationMemory key={`${props.roleId}:${props.threadId}`} {...props} />;
}

function ConversationMemory({ roleId, threadId, title, refresh = 0, onBack }: Props) {
  const base = `/api/roles/${encodeURIComponent(roleId)}/channels/threads/${encodeURIComponent(threadId)}/external-memory`;
  const [draft, setDraft] = useState("");
  const [selected, setSelected] = useState<Member | null>(null);
  const document = useDocument<Conversation>(base, refresh, value => setDraft(value.note));
  const data = document.value;
  const back = useRef<HTMLButtonElement>(null);
  const selectedButton = useRef<HTMLButtonElement | null>(null);
  useEffect(() => { back.current?.focus(); }, []);
  const closeMember = () => { setSelected(null); void document.load(); requestAnimationFrame(() => selectedButton.current?.focus()); };
  return <section className="group-settings-page external-memory-page" aria-label="聊天信息">
    <div hidden={Boolean(selected)} className="external-memory-conversation">
      <header><button ref={back} type="button" className="group-settings-back" onClick={onBack}>返回聊天</button><h2>聊天信息</h2></header>
      <div className="group-settings-scroll">
        <h3>{title}</h3>
        {document.error && <p role="alert" className="group-settings-error">{document.error}</p>}
        {!data && !document.error && <p role="status">正在读取资料…</p>}
        {(!data || document.error) && <button type="button" onClick={() => void document.load(true)}>{data && !document.denied ? "重新读取并保留草稿" : "重试"}</button>}
        {data && !document.denied && <>
          {data.readOnly && <p>账号已移除，历史资料只能查看。</p>}
          <section className="external-memory-card"><h3>最近动态</h3><p>{data.activity.text || "暂无"}</p>{data.activity.updatedAt && <time dateTime={data.activity.updatedAt}>{new Date(data.activity.updatedAt).toLocaleString()}</time>}</section>
          <section className="external-memory-card">
            <label>会话笔记<textarea aria-label="会话笔记" value={draft} maxLength={12000} readOnly={data.readOnly} onChange={event => { document.dirty.current = true; setDraft(event.target.value); }} /></label>
            {document.remote && <details open><summary>当前已保存的笔记</summary><p>{document.remote.note || "（空）"}</p></details>}
            {!data.readOnly && <div className="external-memory-actions"><button type="button" disabled={document.busy} onClick={async () => {
              if (await document.save(base + "/note", { note: draft })) void document.load();
            }}>保存笔记</button><button type="button" disabled={document.busy} onClick={document.restore}>还原笔记</button></div>}
          </section>
          <section className="external-memory-card"><h3>成员</h3><p>仅显示本会话已观察到且有档案的成员。</p>
            <ul className="group-settings-directory">{(document.remote?.members ?? data.members ?? []).map(member => <li key={member.key}><button type="button" onClick={event => { selectedButton.current = event.currentTarget; setSelected(member); }}><strong>{member.name}</strong><small>{member.state === "conflict" ? "需整理" : member.shorthand || `${channelName[member.channel] || member.channel} · ${member.senderId}`}</small><span>›</span></button></li>)}</ul>
            {(document.remote?.members ?? data.members ?? []).length === 0 && <p>暂无成员档案</p>}
          </section>
        </>}
      </div>
    </div>
    {selected && <MemberEditor key={selected.key} base={base} memberKey={selected.key} refresh={refresh} onBack={closeMember} onSaved={() => void document.load()} />}
  </section>;
}

function MemberEditor({ base, memberKey, refresh, onBack, onSaved }: { base: string; memberKey: string; refresh: number; onBack: () => void; onSaved: () => void }) {
  const [profile, setProfile] = useState(""), [shorthand, setShorthand] = useState("");
  const [deleting, setDeleting] = useState(false);
  const url = `${base}/members/${encodeURIComponent(memberKey)}`;
  const document = useDocument<Member>(url, refresh, value => { setProfile(value.profile); setShorthand(value.shorthand); });
  const member = document.value;
  const back = useRef<HTMLButtonElement>(null);
  useEffect(() => { back.current?.focus(); }, []);
  return <section className="group-settings-page" aria-label="成员档案">
    <header><button ref={back} type="button" className="group-settings-back" onClick={onBack} aria-label="返回聊天信息">返回</button><h2>成员档案</h2></header>
    <div className="group-settings-scroll">
      {document.error && <p role="alert" className="group-settings-error">{document.error}</p>}
      {!member && !document.error && <p role="status">正在读取档案…</p>}
      {document.error && <button type="button" onClick={() => void document.load(true)}>{document.denied ? "重试" : "重新读取并保留草稿"}</button>}
      {member && !document.denied && <>
        <div><h3>{member.name}</h3><p>{channelName[member.channel] || member.channel} · {member.senderId}</p><p>昵称历史（只读）</p><p>{member.nicknames.join(" · ") || "暂无"}</p></div>
        <section className="external-memory-card"><h3>出现过的会话</h3><ul>{member.sources.map(source => <li key={source.threadId}>{source.title || source.threadId}</li>)}</ul></section>
        {member.state === "conflict" && <section className="external-memory-card"><h3>需整理</h3><p>旧档案内容不同，确认前不会作为共同事实使用。选择候选或编辑合并后保存。</p>{member.candidates.map(candidate => <article key={candidate.id}><h4>{member.sources.find(source => source.threadId === candidate.threadId)?.title || candidate.threadId}</h4><p>{candidate.profile}</p><p>{candidate.shorthand}</p>{!member.readOnly && <button type="button" onClick={() => { document.dirty.current = true; setProfile(candidate.profile); setShorthand(candidate.shorthand); }}>采用此候选</button>}</article>)}</section>}
        {member.readOnly && <p>账号已移除，历史资料只能查看。</p>}
        <label>速记<textarea aria-label="速记" maxLength={200} readOnly={member.readOnly} value={shorthand} onChange={event => { document.dirty.current = true; setShorthand(event.target.value); }} /></label>
        <label>完整档案<textarea aria-label="完整档案" maxLength={8000} readOnly={member.readOnly} value={profile} onChange={event => { document.dirty.current = true; setProfile(event.target.value); }} /></label>
        {document.remote && <details open><summary>当前已保存的档案</summary><p>{document.remote.shorthand}</p><p>{document.remote.profile || "（空）"}</p></details>}
        {!member.readOnly && <><div className="external-memory-actions"><button type="button" disabled={document.busy} onClick={async () => { if (await document.save(url, { profile, shorthand })) onSaved(); }}>保存档案</button><button type="button" disabled={document.busy} onClick={document.restore}>还原档案</button><button type="button" disabled={document.busy} onClick={() => setDeleting(true)}>删除档案</button></div>
          {deleting && <section role="alertdialog" aria-label="确认删除共享档案" className="external-memory-card"><p>多个会话共用这份档案，删除会同时影响这些会话，聊天记录会保留。后续正常整理可能重新形成档案。</p><div className="external-memory-actions"><button type="button" disabled={document.busy} onClick={async () => { if (await document.save(`${url}?expectedRevision=${member.revision}`, {}, "DELETE")) onBack(); }}>确认删除</button><button type="button" onClick={() => setDeleting(false)}>取消</button></div></section>}
        </>}
      </>}
    </div>
  </section>;
}
