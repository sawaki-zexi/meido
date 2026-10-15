import type { FormEvent } from "react";
import type { RolesState } from "./useRoles";
import type { RoleProfile } from "../../api/types";
import { useRef, useState } from "react";
import { IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { AvatarCropper } from "./AvatarCropper";
import { ShellSettings } from "./ShellSettings";
import { ToolCatalog } from "./ToolCatalog";
import { BasicToolSettings } from "./BasicToolSettings";
import { ScreenPerceptionSettings } from "./ScreenPerceptionSettings";
import { draftFromRole, payloadFromDraft } from "./draft";

type TextFieldKey = keyof RoleProfile | "name" | "description";
type Field = { key: TextFieldKey; label: string; placeholder?: string; hint?: string; multiline?: boolean; required?: boolean };

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
  const cropping = Boolean(state.avatarSource || state.cardImageSource);
  const locked = !writable || busy;
  const toolsChanged = selected !== null && JSON.stringify(payloadFromDraft(draft).agentConfig) !== JSON.stringify(payloadFromDraft(draftFromRole(selected)).agentConfig);
  const [showAdvanced, setShowAdvanced] = useState(!creating);
  const avatarInput = useRef<HTMLInputElement>(null);
  const cardInput = useRef<HTMLInputElement>(null);
  const importInput = useRef<HTMLInputElement>(null);

  const submit = (event: FormEvent) => { event.preventDefault(); if (!cropping) onSubmit(); };

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
      {writable && <div className="role-import-control">
        <button type="button" className="ghost" disabled={busy} onClick={() => importInput.current?.click()}>{state.importing ? "解析中…" : "导入角色卡"}</button>
        <input ref={importInput} className="image-file-input" aria-label="导入角色卡" type="file" accept=".json,.png,.apng,.charx,application/json,image/png" disabled={busy} onChange={(event) => { void state.previewImport(event.currentTarget.files?.[0] ?? null); event.currentTarget.value = ""; }} />
      </div>}
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
      <legend>身份</legend>
      {state.avatarSource && <AvatarCropper file={state.avatarSource} shape="circle" onCancel={state.cancelAvatarCrop} onAccept={state.acceptAvatarCrop} onError={setError} />}
      {state.cardImageSource && <AvatarCropper file={state.cardImageSource} shape="card" onCancel={state.cancelCardImageCrop} onAccept={(image) => state.acceptCardImageCrop(image)} onError={setError} />}
      {identityFields.map(renderField)}
    </fieldset>
    <fieldset>
      <legend>角色设定</legend>
      {renderField(characterFields[0])}
      <button
        type="button"
        className="section-toggle"
        aria-expanded={showAdvanced}
        aria-controls={showAdvanced ? "role-advanced-fields" : undefined}
        onClick={() => setShowAdvanced((current) => !current)}
      >
        {showAdvanced ? "收起更多设定" : "更多设定"}
      </button>
      {showAdvanced && <div id="role-advanced-fields" className="advanced-fields">
        {characterFields.slice(1).map(renderField)}
      </div>}
    </fieldset>
    <fieldset>
      <legend>能力</legend>
      <ScreenPerceptionSettings draft={draft} onChange={setDraft} disabled={locked} />
      <label className="field checkbox-field">
        <input
          type="checkbox"
          aria-label="允许主动检索记忆"
          checked={draft.memoryRecallEnabled}
          disabled={locked}
          onChange={(event) => setDraft({ ...draft, memoryRecallEnabled: event.target.checked })}
        />
        <span>允许主动检索记忆</span>
        <small className="field-hint">关闭时仍保留每轮自动记忆召回，但模型不能主动查询记忆。</small>
      </label>
      <label className="field checkbox-field">
        <input
          type="checkbox"
          aria-label="允许浏览器操作网页"
          checked={draft.browserUseEnabled}
          disabled={locked}
          onChange={(event) => setDraft({ ...draft, browserUseEnabled: event.target.checked })}
        />
        <span>允许浏览器操作网页</span>
        <small className="field-hint">启用后角色可以在独立浏览器中访问网页、执行交互并读取截图。</small>
      </label>
      <label className="field checkbox-field">
        <input
          type="checkbox"
          aria-label="允许查询主人待办"
          checked={draft.todoSearchEnabled}
          disabled={locked}
          onChange={(event) => setDraft({ ...draft, todoSearchEnabled: event.target.checked })}
        />
        <span>允许查询主人待办</span>
        <small className="field-hint">只允许读取与当前问题相关的事项。</small>
      </label>
    </fieldset>
    <BasicToolSettings draft={draft} onChange={setDraft} disabled={locked} />
    <ShellSettings draft={draft} onChange={setDraft} disabled={locked} />
    {selected && !creating && <ToolCatalog key={`${selected.id}:${selected.updatedAt}`} roleId={selected.id} hasUnsavedChanges={toolsChanged} />}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {state.importPreview && <section className="role-import-preview" aria-label="角色卡导入预览">
      <div className="role-import-preview-header"><strong>导入预览</strong><button type="button" className="ghost" onClick={state.cancelImport}>取消</button></div>
      <p className="role-import-source">{state.importPreview.source} · {state.importPreview.compatibility.format.toUpperCase()}</p>
      <dl className="role-import-summary">
        {(["name", "description", "profile", "personality", "behaviorRules", "responseConstraints", "nickname"] as const).map((key) => {
          const labels = { name: "名称", description: "简介", profile: "角色设定", personality: "性格", behaviorRules: "行为规则", responseConstraints: "回复约束", nickname: "称呼" };
          return <div key={key}><dt>{labels[key]}</dt><dd>{state.importPreview!.draft[key] || "未填写"}</dd></div>;
        })}
      </dl>
      {state.importPreview.assets.length > 0 && <div className="role-import-assets"><span className="field-label">素材（头像会进入头像裁剪；其他图片可设为卡片图）</span>{state.importPreview.assets.map((asset, index) => <button type="button" className="role-import-asset" key={`${asset.kind}:${asset.name}:${index}`} disabled={!asset.dataUrl} onClick={() => void state.acceptImport(asset)}>{asset.dataUrl ? <img src={asset.dataUrl} alt="" /> : null}<span>{asset.name || "未命名素材"}</span><small>{asset.kind === "avatar" ? "头像" : asset.kind === "background" ? "背景 · 卡片图" : asset.kind === "emotion" ? "情绪图 · 卡片图" : "素材 · 卡片图"}</small></button>)}</div>}
      {state.importPreview.compatibility.warnings.length > 0 && <ul className="role-import-warnings">{state.importPreview.compatibility.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
      {state.importPreview.compatibility.discardedFields.length > 0 && <p className="role-import-discarded">未映射字段：{state.importPreview.compatibility.discardedFields.join("、")}</p>}
      {state.importPreview.compatibility.unsupportedMacros.length > 0 && <p className="role-import-discarded">未处理宏：{state.importPreview.compatibility.unsupportedMacros.join("、")}</p>}
      {state.importPreview.compatibility.unsupportedResources.length > 0 && <p className="role-import-discarded">未导入素材：{state.importPreview.compatibility.unsupportedResources.join("、")}</p>}
      <div className="form-actions"><button type="button" className="primary" onClick={() => void state.acceptImport(null)}>填充到表单</button></div>
    </section>}
    {writable && !cropping && <div className="form-actions">
      <button type="button" className="ghost" disabled={busy} onClick={onCancel}>取消</button>
      {selected && !creating && <IconButton icon="trash" label="删除角色" className="danger" disabled={deleting || busy} onClick={onDelete} />}
      <button type="submit" className="primary" disabled={busy}>{saving ? "保存中…" : "保存"}</button>
    </div>}
  </form>;
}
