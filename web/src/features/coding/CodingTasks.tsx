import { useEffect, useState } from "react";
import { cancelCodingTask, listCodingTasks, type CodingTask } from "../../api/coding";
import { errorMessage } from "../../api/client";

export function CodingTasks({ roleId }: { roleId: string }) {
  const [tasks, setTasks] = useState<CodingTask[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const refresh = () => { void listCodingTasks(roleId).then(data => { if (live) { setTasks(data.tasks); setError(""); } }).catch(cause => { if (live) setError(errorMessage(cause, "编程任务读取失败")); }); };
    refresh(); const interval = setInterval(refresh, 1500);
    return () => { live = false; clearInterval(interval); };
  }, [roleId]);
  const cancel = async (task: CodingTask) => {
    setBusy(task.id);
    try { await cancelCodingTask(roleId, task.id); setTasks((await listCodingTasks(roleId)).tasks); }
    catch (cause) { setError(errorMessage(cause, "取消未确认，请保留任务记录")); }
    finally { setBusy(null); }
  };
  return <section aria-label="编程任务"><h3>编程任务</h3>{error && <p role="alert">{error}</p>}{!tasks.length && <p>尚无任务。可在聊天中给出已授权仓库和完整需求。</p>}{tasks.map(task => <article key={task.attemptId}><strong>{task.id}</strong><p>任务：{task.status} · 运行：{task.runtimeStatus} · 资源：{task.cleanupState} · 交付：{task.deliveryStatus}</p>{task.summary && <p>{task.summary}</p>}{task.branch && <p>分支：{task.branch}</p>}{task.blockers?.length ? <p>{task.blockers.join("；")}</p> : null}{(["accepted", "running"].includes(task.status) || task.cleanupState === "cleanup_failed") && <button type="button" disabled={busy !== null} onClick={() => void cancel(task)}>{busy === task.id ? "正在确认资源退出…" : task.cleanupState === "cleanup_failed" ? "重新确认资源退出" : "取消任务"}</button>}</article>)}</section>;
}
