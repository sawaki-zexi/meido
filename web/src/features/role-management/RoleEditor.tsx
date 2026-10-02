import type { FormEvent } from "react";
import type { RolesState } from "./useRoles";
import type { RoleDraft } from "./draft";
import { Avatar } from "../../ui/Avatar";
import { IconButton } from "../../ui/Icon";
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

type Props = { state: RolesState; onSubmit: () => void; onCancel: () => void; onDelete: () => void };

/** Role profile: read-only by default, editable after "编辑", or a blank form when creating. */
export function RoleEditor({ state, onSubmit, onCancel, onDelete }: Props) {
  const { selected, draft, setDraft, creating, editing, saving, deleting, busy, error, setError } = state;
  const writable = creating || editing;
  const locked = !writable || busy;

  const submit = (event: FormEvent) => { event.preventDefault(); onSubmit(); };

  const renderField = (field: Field) => {
    const id = `role-${field.key}`;
    const common = { id, required: field.required, disabled: locked, placeholder: writable ? undefined : "未填写", value: draft[field.key], onChange: (event: { target: { value: string } }) => setDraft({ ...draft, [field.key]: event.target.value }) };
    return <div className="field" key={field.key}>
      <label htmlFor={id}>{field.label}{field.required && <span className="required" aria-hidden="true">*</span>}</label>
      {field.hint && <small className="field-hint">{field.hint}</small>}
      {field.multiline ? <textarea {...common} /> : <input {...common} />}
    </div>;
  };

  return <form className={`role-editor${writable ? " is-writable" : ""}`} onSubmit={submit}>
    {selected && !creating && <header className="profile-card">
      <Avatar id={selected.id} name={draft.name || selected.name} size="lg" />
      <div>
        <strong>{draft.name || selected.name}</strong>
        <small className="muted">{editing ? "修改只影响之后的对话" : selected.description || "暂无简介"}</small>
      </div>
      {!editing && <span className="profile-actions">
        <IconButton icon="edit" label="编辑" onClick={state.startEdit} />
        <IconButton icon="trash" label="删除角色" className="danger" disabled={deleting} onClick={onDelete} />
      </span>}
    </header>}
    {creating && <p className="muted">填写后保存，就可以和她开始对话。</p>}
    <fieldset>
      <legend>身份</legend>
      {identityFields.map(renderField)}
    </fieldset>
    <fieldset>
      <legend>设定</legend>
      {characterFields.map(renderField)}
    </fieldset>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {writable && <div className="form-actions">
      <button type="button" className="ghost" disabled={busy} onClick={onCancel}>取消</button>
      <button type="submit" className="primary" disabled={busy}>{saving ? "保存中…" : "保存"}</button>
    </div>}
  </form>;
}
