import { useState, type FormEvent } from "react";
import { Notice, Placeholder } from "../../ui/Status";
import { useCodingTasks } from "./useCodingTasks";

export function CodingTasks({ roleId }: { roleId: string }) {
  return <CodingTaskPanel key={roleId} roleId={roleId} />;
}

function CodingTaskPanel({ roleId }: { roleId: string }) {
  const state = useCodingTasks(roleId);
  const [message, setMessage] = useState("");
  const [authorName, setAuthorName] = useState("");
  const [authorEmail, setAuthorEmail] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const prepare = (event: FormEvent) => {
    event.preventDefault();
    void state.prepare(state.selection?.action === "commit" ? { action: "commit", message, authorName, authorEmail } : { action: "publish", title, body });
  };
  const review = state.review;
  return <section aria-label="编程任务"><h3>编程任务</h3>
    {state.error && <Notice onDismiss={state.dismiss}>{state.error}</Notice>}
    {state.loading ? <Placeholder loading>正在读取编程成果…</Placeholder> : !state.tasks.length && <Placeholder>尚无任务。可在聊天中给出已授权仓库和完整需求。</Placeholder>}
    <button type="button" disabled={state.busy} onClick={() => void state.refresh()}>刷新编程状态</button>
    {state.tasks.map(task => <article key={task.attemptId}><strong>{task.id}</strong>
      <p>任务：{task.status} · 运行：{task.runtimeStatus} · 资源：{task.cleanupState} · 交付：{task.deliveryStatus}</p>
      {task.summary && <p>{task.summary}</p>}{task.branch && <p>分支：{task.branch}</p>}
      {task.expectedHead && <p>宿主确认 HEAD：<code>{task.expectedHead}</code></p>}
      {task.blockers?.length ? <p>{task.blockers.join("；")}</p> : null}
      {task.validation?.map(value => <p key={value.id}>{value.id}：{value.status}</p>)}
      {(["accepted", "running"].includes(task.status) || task.cleanupState === "cleanup_failed") && <button type="button" disabled={state.busy} onClick={() => void state.cancelTask(task)}>{task.cleanupState === "cleanup_failed" ? "重新确认资源退出" : "取消任务"}</button>}
      {task.status === "validated_diff" && <button type="button" aria-label={`审阅本地提交 ${task.id}`} disabled={state.busy || !task.ownerActions?.commitAllowed || !task.ownerActions.hasEvidence} onClick={() => state.select(task, "commit")}>审阅本地提交</button>}
      {task.status === "validated_commit" && <button type="button" aria-label={`审阅 Draft PR ${task.id}`} disabled={state.busy || !task.ownerActions?.publicationAllowed} onClick={() => state.select(task, "publish")}>审阅 Draft PR</button>}
      {task.status === "validated_diff" && <p>本地提交受原任务预算约束。预算结束时成果保留；请回到原来源明确提出新的继续要求，CLI 可能再次消耗配额，系统不会自动启动。</p>}
    </article>)}
    {state.selection && <form onSubmit={prepare} onChange={state.clearReview}><fieldset disabled={state.busy}><legend>{state.selection.action === "commit" ? "准备本地提交" : "准备 Draft PR"}</legend>
      {state.selection.action === "commit" ? <>
        <label className="field">提交说明<textarea aria-label="提交说明" required maxLength={8000} value={message} onChange={event => setMessage(event.target.value)} /></label>
        <label className="field">提交作者<input aria-label="提交作者" required maxLength={100} value={authorName} onChange={event => setAuthorName(event.target.value)} /></label>
        <label className="field">提交邮箱<input aria-label="提交邮箱" type="email" required maxLength={254} value={authorEmail} onChange={event => setAuthorEmail(event.target.value)} /></label>
      </> : <>
        <label className="field">PR 标题<input aria-label="PR 标题" required maxLength={200} value={title} onChange={event => setTitle(event.target.value)} /></label>
        <label className="field">PR 正文<textarea aria-label="PR 正文" maxLength={32768} value={body} onChange={event => setBody(event.target.value)} /></label>
      </>}<button type="submit">准备审阅</button>
    </fieldset></form>}
    {review && <section aria-label="确切成果审阅" className="coding-review"><h4>批准前核对</h4>
      <dl><dt>任务 / 尝试</dt><dd>{review.taskId} / {review.attemptId}</dd><dt>原生会话</dt><dd>{review.nativeSession}</dd>
        {review.source && <><dt>原角色 / 来源</dt><dd>{review.source.role_id} / {review.source.session_key}{review.source.source_account_id && ` / 账号 ${review.source.source_account_id}`}</dd></>}
        <dt>原始 base HEAD</dt><dd><code>{review.baseHead}</code></dd><dt>当前 HEAD</dt><dd><code>{review.head}</code></dd>
        <dt>拟提交 / 发布 SHA</dt><dd><code>{review.commitSha}</code></dd><dt>工作分支</dt><dd>{review.branch}</dd></dl>
      {review.attestedAt && <p>宿主验证时间：{new Date(review.attestedAt * 1000).toLocaleString()}</p>}
      <pre className="coding-review-diff" aria-label="实际成果差异">{review.diff}</pre>
      {review.validation?.map(value => <p key={value.id}>{value.id}：{value.status}（退出码 {value.exitCode}），目录 {value.cwd}，固定命令 <code>{JSON.stringify(value.argv)}</code></p>)}
      {review.action === "commit" ? <p>{review.authorName} &lt;{review.authorEmail}&gt;：{review.message}</p> : <>
        <p>远端：{review.remote?.url}</p><p>目标 base：{review.baseRef}</p><p>发布分支：{review.publishBranch}</p>
        <p>Draft PR：{review.title}</p><pre className="coding-review-diff">{review.body}</pre>
        {review.pushRequired === false && <p>此次只批准创建 Draft PR；原发布分支已核实，不会再次推送。原操作：{review.previousOperationId}</p>}
      </>}
      <label className="checkbox-field"><input type="checkbox" checked={state.consent} disabled={state.busy} onChange={event => state.setConsent(event.target.checked)} />我已审阅此次确切成果、验证与目标，仅批准此次{review.action === "commit" ? "本地提交" : review.pushRequired === false ? "剩余 Draft PR 创建" : "推送与创建 Draft PR"}</label>
      <button type="button" disabled={state.busy || !state.consent} onClick={() => void state.approve()}>{review.action === "commit" ? "批准此次本地提交" : "批准此次发布"}</button>
    </section>}
    {!!state.operations.length && <section aria-label="宿主操作结果"><h4>宿主操作</h4>{state.operations.map(operation => <article key={operation.id}>
      <strong>{operation.id}</strong><p><span>{operation.status}</span> · 资源：{operation.cleanupState} · 原来源交付：{operation.deliveryStatus}</p>
      {operation.commitSha && <p>确切提交：<code>{operation.commitSha}</code></p>}
      {operation.prUrl && <a href={operation.prUrl} target="_blank" rel="noreferrer">查看已核验 Draft PR</a>}
      {operation.status === "unknown" && <><p>远端结果不确定。只读核对不会重新推送或创建 PR，取消也不会回滚已发生的事务。</p><button type="button" disabled={state.busy} onClick={() => void state.observe(operation)}>只读核对 {operation.id}</button></>}
      {operation.status === "push_confirmed" && <><p>已核实推送完成，PR 尚未派发。需要重新审阅和批准剩余创建动作。</p><button type="button" disabled={state.busy} onClick={() => state.selectRemaining(operation)}>审阅剩余 PR {operation.id}</button></>}
      {(["accepted", "running"].includes(operation.status) || operation.cleanupState === "cleanup_failed") && <button type="button" disabled={state.busy} onClick={() => void state.cancelOperation(operation)}>停止宿主操作 {operation.id}</button>}
      {!!operation.blockers?.length && <p>{operation.blockers.join("；")}</p>}
    </article>)}</section>}
  </section>;
}
