import type { FormEvent } from "react";
import type { RolesState } from "./useRoles";
import type { RoleDraft } from "./draft";
import { useRef } from "react";
import { IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { AvatarCropper } from "./AvatarCropper";

type Field = { key: keyof RoleDraft; label: string; placeholder: string; multiline?: boolean; required?: boolean };

const identityFields: Field[] = [
  { key: "name", label: "名称", placeholder: "给她起个名字", required: true },
  { key: "nickname", label: "昵称", placeholder: "角色对你的称呼，例如：主人" },
  { key: "description", label: "简介", placeholder: "简短介绍她，让你在角色卡上一眼认出她", multiline: true },
];
const characterFields: Field[] = [
  { key: "profile", label: "角色设定", placeholder: "描述她的身份、背景，以及你们之间的关系", multiline: true, required: true },
  { key: "personality", label: "性格", placeholder: "例如：温柔、细心，偶尔会开些小玩笑", multiline: true },
  { key: "behaviorRules", label: "行为规则", placeholder: "描述她平时会怎么做、怎么回应你", multiline: true },
  { key: "responseConstraints", label: "回复约束", placeholder: "例如：避免替你做决定，回答尽量简洁", multiline: true },
];

type Props = { state: RolesState; onSubmit: () => void; onCancel: () => void; onDelete: () => void };

/** Role profile opens directly in edit mode; creation uses the same form. */
export function RoleEditor({ state, onSubmit, onCancel, onDelete }: Props) {
  const { selected, draft, setDraft, creating, editing, saving, deleting, busy, error, setError } = state;
  const writable = creating || editing;
  const locked = !writable || busy;
  const avatarInput = useRef<HTMLInputElement>(null);
  const cardInput = useRef<HTMLInputElement>(null);

  const submit = (event: FormEvent) => { event.preventDefault(); onSubmit(); };

  const renderField = (field: Field) => {
    const id = `role-${field.key}`;
    const common = { id, required: field.required, disabled: locked, placeholder: writable ? field.placeholder : "未填写", value: draft[field.key], onChange: (event: { target: { value: string } }) => setDraft({ ...draft, [field.key]: event.target.value }) };
    return <div className="field" key={field.key}>
      <label htmlFor={id}>{field.label}{field.required && <span className="required" aria-hidden="true">*</span>}</label>
      {field.multiline ? <textarea {...common} /> : <input {...common} />}
    </div>;
  };

  return <form className={`role-editor${writable ? " is-writable" : ""}`} onSubmit={submit}>
    <fieldset>
      <div className="role-image-settings">
        <section className="image-control">
          <span className="field-label">头像</span>
          {writable && <button type="button" className="image-add" aria-label="上传头像" disabled={locked} onClick={() => avatarInput.current?.click()}>{state.avatarPreviewUrl || (selected?.avatarUrl && !state.avatarRemoved) ? <img src={state.avatarPreviewUrl ?? selected?.avatarUrl ?? ""} alt="" /> : <span aria-hidden="true">+</span>}</button>}
          <input ref={avatarInput} className="image-file-input" aria-label="角色头像" type="file" accept="image/png,image/jpeg,image/webp" disabled={locked} onChange={(event) => { state.chooseAvatar(event.currentTarget.files?.[0] ?? null); event.currentTarget.value = ""; }} />
          {(selected?.avatarUrl || state.avatarFile) && <button type="button" className="ghost image-remove" disabled={locked} onClick={state.clearAvatar}>移除</button>}
        </section>
        <section className="image-control">
          <span className="field-label">卡片图</span>
          {writable && <button type="button" className="image-add" aria-label="上传卡片图片" disabled={locked} onClick={() => cardInput.current?.click()}>{state.cardImagePreviewUrl || (selected?.cardImageUrl && !state.cardImageRemoved) ? <img src={state.cardImagePreviewUrl ?? selected?.cardImageUrl ?? ""} alt="" /> : <span aria-hidden="true">+</span>}</button>}
          <input ref={cardInput} className="image-file-input" aria-label="卡片图片" type="file" accept="image/png,image/jpeg,image/webp" disabled={locked} onChange={(event) => { state.chooseCardImage(event.currentTarget.files?.[0] ?? null); event.currentTarget.value = ""; }} />
          {(selected?.cardImageUrl || state.cardImageFile) && <button type="button" className="ghost image-remove" disabled={locked} onClick={state.clearCardImage}>恢复头像</button>}
        </section>
      </div>
    </fieldset>
    <fieldset>
      {state.avatarSource && <AvatarCropper file={state.avatarSource} shape="circle" onCancel={state.cancelAvatarCrop} onAccept={state.acceptAvatarCrop} onError={setError} />}
      {state.cardImageSource && <AvatarCropper file={state.cardImageSource} onCancel={state.cancelCardImageCrop} onAccept={(image) => state.acceptCardImageCrop(image)} onError={setError} />}
      {identityFields.map(renderField)}
    </fieldset>
    <fieldset>
      {characterFields.map(renderField)}
    </fieldset>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {writable && <div className="form-actions">
      <button type="button" className="ghost" disabled={busy} onClick={onCancel}>取消</button>
      {selected && !creating && <IconButton icon="trash" label="删除角色" className="danger" disabled={deleting || busy} onClick={onDelete} />}
      <button type="submit" className="primary" disabled={busy}>{saving ? "保存中…" : "保存"}</button>
    </div>}
  </form>;
}
