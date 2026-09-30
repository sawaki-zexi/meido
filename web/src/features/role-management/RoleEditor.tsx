import type { FormEvent } from "react";
import type { RolesState } from "./useRoles";
import type { RoleDraft } from "./draft";
import { Avatar } from "../../ui/Avatar";
import { Notice } from "../../ui/Status";

type Field = { key: keyof RoleDraft; label: string; hint?: string; multiline?: boolean; required?: boolean };

const identityFields: Field[] = [
  { key: "name", label: "名称", required: true },
  { key: "nickname", label: "昵称", hint: "她如何称呼你" },
  { key: "description", label: "简介", multiline: true },
];
const characterFields: Field[] = [
  { key: "profile", label: "角色设定", hint: "身份、背景和基本设定", multiline: true, required: true },
  { key: "personality", label: "性格", multiline: true },
  { key: "behaviorRules", label: "行为规则", multiline: true },
  { key: "responseConstraints", label: "回复约束", multiline: true },
];

type Props = { state: RolesState; opening: boolean; onOpenChat: () => void; onDelete: () => void };

export function RoleEditor({ state, opening, onOpenChat, onDelete }: Props) {
  const { selected, draft, setDraft, creating, editing, saving, deleting, busy, error, setError } = state;
  const writable = creating || editing;
  const locked = !writable || busy;

  const submit = (event: FormEvent) => { event.preventDefault(); void state.save(); };

  const renderField = (field: Field) => {
    const id = `role-${field.key}`;
    const common = { id, required: field.required, disabled: locked, placeholder: writable ? undefined : "未填写", value: draft[field.key], onChange: (event: { target: { value: string } }) => setDraft({ ...draft, [field.key]: event.target.value }) };
    return <div className="field" key={field.key}>
      <label htmlFor={id}>{field.label}{field.required && <span className="required" aria-hidden="true">*</span>}</label>
      {field.hint && <small className="field-hint">{field.hint}</small>}
      {field.multiline ? <textarea {...common} /> : <input {...common} />}
    </div>;
  };

  const title = creating ? "创建角色" : "角色详情";
  return <form className={`role-editor${writable ? " is-writable" : ""}`} onSubmit={submit} aria-label={title}>
    <header className="editor-header">
      {selected && !creating ? <Avatar id={selected.id} name={draft.name || selected.name} size="lg" /> : <span className="avatar avatar-lg avatar-new" aria-hidden="true">＋</span>}
      <div>
        <h2>{title}</h2>
        {selected && !creating && <small className="muted">角色 ID · {selected.id}</small>}
        {writable && <small className="muted">{creating ? "填写后保存即可开始对话" : "修改只影响之后的对话"}</small>}
      </div>
    </header>
    <fieldset>
      <legend>身份</legend>
      {identityFields.map(renderField)}
    </fieldset>
    <fieldset>
      <legend>设定</legend>
      {characterFields.map(renderField)}
    </fieldset>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    <div className="form-actions">
      {writable ? <>
        <button type="submit" className="primary" disabled={busy}>{saving ? "保存中…" : "保存"}</button>
        <button type="button" disabled={busy} onClick={state.cancel}>取消</button>
      </> : <>
        <button type="button" onClick={state.startEdit}>编辑</button>
        <button type="button" className="danger" disabled={deleting} onClick={onDelete}>{deleting ? "删除中…" : "删除角色"}</button>
      </>}
      {selected && <button type="button" className="primary push-end" disabled={busy || opening} onClick={onOpenChat}>{opening ? "正在打开…" : "进入会话"}</button>}
    </div>
  </form>;
}
