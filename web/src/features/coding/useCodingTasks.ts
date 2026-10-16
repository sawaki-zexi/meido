import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import { approveCodingReview, cancelCodingOperation, cancelCodingTask, listCodingOperations, listCodingTasks, observeCodingOperation, prepareCodingReview, type CodingOperation, type CodingReview, type CodingReviewInput, type CodingTask } from "../../api/coding";

export function useCodingTasks(roleId: string) {
  const identity = useRef({ roleId, generation: 0 });
  if (identity.current.roleId !== roleId) identity.current = { roleId, generation: identity.current.generation + 1 };
  const alive = useRef(true), inFlight = useRef(false);
  const [data, setData] = useState<{ roleId: string; tasks: CodingTask[]; operations: CodingOperation[] } | null>(null);
  const [selection, setSelection] = useState<{ roleId: string; task: CodingTask; action: "commit" | "publish"; previousOperationId?: string } | null>(null);
  const [prepared, setPrepared] = useState<{ roleId: string; value: CodingReview; requestId: string } | null>(null);
  const [consent, setConsent] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState({ message: "", authorName: "", authorEmail: "", title: "", body: "" });
  const current = useCallback((generation: number) => alive.current && identity.current.roleId === roleId && identity.current.generation === generation, [roleId]);
  const refresh = useCallback(async () => {
    const generation = identity.current.generation;
    try {
      const [tasks, operations] = await Promise.all([listCodingTasks(roleId), listCodingOperations(roleId)]);
      if (!current(generation)) return;
      setData({ roleId, tasks: tasks.tasks, operations: operations.operations });
      setPrepared(previous => {
        if (!previous || previous.roleId !== roleId) return previous;
        const actual = tasks.tasks.find(task => task.id === previous.value.taskId);
        const head = actual?.expectedHead ?? actual?.validatedHead;
        if (!actual || actual.attemptId !== previous.value.attemptId || head && head !== previous.value.head
          || previous.value.action === "commit" && !actual.ownerActions?.commitAllowed
          || previous.value.action === "publish" && !actual.ownerActions?.publicationAllowed) return null;
        return previous;
      });
    } catch (cause) { if (current(generation)) setError(errorMessage(cause, "编程状态暂不可读，请查询原操作")); }
  }, [roleId, current]);
  useEffect(() => {
    alive.current = true;
    setSelection(null); setPrepared(null); setConsent(false); setError("");
    setDraft({ message: "", authorName: "", authorEmail: "", title: "", body: "" });
    void refresh();
    const interval = setInterval(() => { void refresh(); }, 1500);
    return () => { alive.current = false; clearInterval(interval); };
  }, [refresh]);
  const perform = async (action: () => Promise<unknown>, success?: () => void) => {
    if (inFlight.current) return;
    const generation = identity.current.generation;
    inFlight.current = true; setBusy(true); setError("");
    try { await action(); if (current(generation)) { success?.(); await refresh(); } }
    catch (cause) { if (current(generation)) setError(errorMessage(cause, "操作结果未确认，请查询原操作；未自动重试")); }
    finally { inFlight.current = false; if (current(generation)) setBusy(false); }
  };
  const prepare = (input: CodingReviewInput) => {
    if (!selection || selection.roleId !== roleId) return;
    const generation = identity.current.generation;
    return perform(async () => {
      const value = await prepareCodingReview(roleId, selection.task.id, input.action === "publish" && selection.previousOperationId ? { ...input, previousOperationId: selection.previousOperationId } : input);
      if (current(generation)) { setPrepared({ roleId, value, requestId: crypto.randomUUID() }); setConsent(false); }
    });
  };
  const approve = () => {
    if (!consent || !prepared || prepared.roleId !== roleId) return;
    return perform(() => approveCodingReview(roleId, prepared.value.id, prepared.requestId), () => { setPrepared(null); setSelection(null); setConsent(false); });
  };
  const select = (task: CodingTask, action: "commit" | "publish", previousOperationId?: string) => { setSelection({ roleId, task, action, previousOperationId }); setPrepared(null); setConsent(false); setError(""); };
  return { tasks: data?.roleId === roleId ? data.tasks : [], operations: data?.roleId === roleId ? data.operations : [],
    loading: data?.roleId !== roleId, selection: selection?.roleId === roleId ? selection : null,
    review: prepared?.roleId === roleId ? prepared.value : null, consent, setConsent, busy, error, dismiss: () => setError(""),
    select, approve, refresh, draft,
    setDraftField: (key: keyof typeof draft, value: string) => { setDraft(previous => ({ ...previous, [key]: value })); setPrepared(null); setConsent(false); },
    prepareDraft: () => prepare(selection?.action === "commit" ? { action: "commit", message: draft.message, authorName: draft.authorName, authorEmail: draft.authorEmail } : { action: "publish", title: draft.title, body: draft.body }),
    selectRemaining: (operation: CodingOperation) => { const task = data?.tasks.find(value => value.id === operation.taskId && value.attemptId === operation.attemptId); if (task) select(task, "publish", operation.id); },
    cancelTask: (task: CodingTask) => perform(() => cancelCodingTask(roleId, task.id)),
    cancelOperation: (operation: CodingOperation) => perform(() => cancelCodingOperation(roleId, operation.id)),
    observe: (operation: CodingOperation) => perform(() => observeCodingOperation(roleId, operation.id)) };
}
