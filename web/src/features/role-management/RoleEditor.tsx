import type { FormEvent } from "react";
import type { RolesState } from "./useRoles";
import type { RoleProfile } from "../../api/types";
import { useEffect, useRef, useState } from "react";
import { Icon } from "../../ui/Icon";
import { RoleDeleteConfirmation } from "./RoleDeleteConfirmation";
import { Notice } from "../../ui/Status";
import { AvatarCropper } from "./AvatarCropper";
import { RoleProfileSections, type ProfileSection } from './RoleProfileSections';

type TextFieldKey = keyof RoleProfile | "name" | "description";
type Field = { key: TextFieldKey; label: string; placeholder?: string; hint?: string; multiline?: boolean; required?: boolean };

const identityFields: Field[] = [
  { key: "name", label: "名称", placeholder: "给她起个名字", required: true },
  { key: "description", label: "简介", placeholder: "简短介绍她，让你在角色卡上一眼认出她", multiline: true },
];
const characterFields: Field[] = [
  { key: "profile", label: "角色设定", placeholder: "描述她的身份、背景，以及你们之间的关系", multiline: true, required: true },
  { key: "personality", label: "性格", placeholder: "例如：温柔、细心，偶尔会开些小玩笑", multiline: true },
  { key: "behaviorRules", label: "行为规则", placeholder: "描述她平时会怎么做、怎么回应你", multiline: true },
  { key: "responseConstraints", label: "回复约束", placeholder: "例如：避免替你做决定，回答尽量简洁", multiline: true },
];

type Props = { state: RolesState; onSubmit: () => void; onCancel: () => void; onDelete: () => void; hideHeaderFields?: boolean; workspace?: boolean; formId?: string; mediaOpen?: boolean };

/** Shared profile fields for creating, reading and editing a role. */
export function RoleEditor({ state, onSubmit, onCancel, onDelete, hideHeaderFields = false, workspace = false, formId, mediaOpen = true }: Props) {
  const { selected, draft, setDraft, creating, editing, saving, deleting, busy, error, setError } = state;
  const writable = workspace || creating || editing;
  const cropping = Boolean(state.avatarSource || state.cardImageSource);
  const locked = !writable || busy;
  const [showAdvanced, setShowAdvanced] = useState(!creating);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [expandedProfile, setExpandedProfile] = useState<ProfileSection | null>('profile');
  const [focusInvalidProfile, setFocusInvalidProfile] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const avatarInput = useRef<HTMLInputElement>(null);
  const cardInput = useRef<HTMLInputElement>(null);
  const importInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusInvalidProfile) { form.current?.querySelector<HTMLTextAreaElement>('textarea[name="profile"]')?.focus(); setFocusInvalidProfile(false); }
  }, [focusInvalidProfile, expandedProfile]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (cropping || busy) return;
    if (workspace && !draft.name.trim()) { setError('请填写角色名称'); document.querySelector<HTMLInputElement>('[aria-label="名称（角色名称）"]')?.focus(); return; }
    if (workspace && !draft.profile.trim()) { setError('请填写角色设定'); setExpandedProfile('profile'); setFocusInvalidProfile(true); return; }
    onSubmit();
  };

  const renderField = (field: Field) => {
    const id = `role-${field.key}`;
    const common = { id, required: field.required, disabled: locked, placeholder: writable ? field.placeholder : "未填写", value: draft[field.key], onChange: (event: { target: { value: string } }) => setDraft({ ...draft, [field.key]: event.target.value }) };
    return <div className="field" key={field.key}>
      <label htmlFor={id}>{field.label}{field.required && <span className="required" aria-hidden="true">*</span>}</label>
      {field.multiline ? <textarea {...common} /> : <input {...common} />}
    </div>;
  };

  return <><form ref={form} id={formId} className={`role-editor${writable ? " is-writable" : ""}`} onSubmit={submit}>
    {workspace && error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    <fieldset className="role-media-fields" hidden={workspace && !mediaOpen}>
      {writable && <div className="role-import-control">
        <div><h2>角色形象</h2><p>头像与卡面，都可以稍后添加。</p></div>
        <button type="button" className="ghost" disabled={busy} onClick={() => importInput.current?.click()}><Icon name="upload" size={15} />{state.importing ? "解析中…" : "导入角色卡"}</button>
        <input ref={importInput} className="image-file-input" aria-label="导入角色卡" type="file" accept=".json,.png,.apng,.charx,application/json,image/png" disabled={busy} onChange={(event) => { void state.previewImport(event.currentTarget.files?.[0] ?? null); event.currentTarget.value = ""; }} />
      </div>}
      <div className="role-image-settings">
        <section className="image-control">
          {writable && <button type="button" className="role-image-picker" aria-label="上传头像" disabled={locked} onClick={() => avatarInput.current?.click()}>
            <span className="image-add">{state.avatarPreviewUrl || (selected?.avatarUrl && !state.avatarRemoved) ? <img src={state.avatarPreviewUrl ?? selected?.avatarUrl ?? ""} alt="" /> : <Icon name="plus" size={20} />}</span>
            <span className="role-image-copy"><strong>头像</strong><small>用于聊天与角色列表</small></span>
          </button>}
          <input ref={avatarInput} className="image-file-input" aria-label="角色头像" type="file" accept="image/png,image/jpeg,image/webp" disabled={locked} onChange={(event) => { state.chooseAvatar(event.currentTarget.files?.[0] ?? null); event.currentTarget.value = ""; }} />
          {(selected?.avatarUrl || state.avatarFile) && <button type="button" className="ghost image-remove" disabled={locked} onClick={state.clearAvatar}>移除</button>}
        </section>
        <section className="image-control">
          {writable && <button type="button" className="role-image-picker" aria-label="上传卡片图片" disabled={locked} onClick={() => cardInput.current?.click()}>
            <span className="image-add is-card">{state.cardImagePreviewUrl || (selected?.cardImageUrl && !state.cardImageRemoved) ? <img src={state.cardImagePreviewUrl ?? selected?.cardImageUrl ?? ""} alt="" /> : <Icon name="image" size={20} />}</span>
            <span className="role-image-copy"><strong>卡片图</strong><small>为她选择一张封面</small></span>
          </button>}
          <input ref={cardInput} className="image-file-input" aria-label="卡片图片" type="file" accept="image/png,image/jpeg,image/webp" disabled={locked} onChange={(event) => { state.chooseCardImage(event.currentTarget.files?.[0] ?? null); event.currentTarget.value = ""; }} />
          {(selected?.cardImageUrl || state.cardImageFile) && <button type="button" className="ghost image-remove" disabled={locked} onClick={state.clearCardImage}>恢复头像</button>}
        </section>
      </div>
    </fieldset>
    {cropping && <fieldset>
      <legend>图片裁切</legend>
      {state.avatarSource && <AvatarCropper file={state.avatarSource} shape="circle" onCancel={state.cancelAvatarCrop} onAccept={state.acceptAvatarCrop} onError={setError} />}
      {state.cardImageSource && <AvatarCropper file={state.cardImageSource} shape="card" onCancel={state.cancelCardImageCrop} onAccept={(image) => state.acceptCardImageCrop(image)} onError={setError} />}
    </fieldset>}
    {!hideHeaderFields && <fieldset>
      <legend>身份</legend>
      {identityFields.map(renderField)}
    </fieldset>}
    {workspace ? <RoleProfileSections profile={draft} expanded={expandedProfile} onExpand={setExpandedProfile} disabled={busy}
      onChange={(key, value) => { if (!editing) state.startEdit(); setDraft({ ...draft, [key]: value }); }} /> : <fieldset className="role-profile-fields">
      {renderField(characterFields[0])}
      <button
        type="button"
        className="section-toggle"
        aria-expanded={showAdvanced}
        aria-controls={showAdvanced ? "role-advanced-fields" : undefined}
        onClick={() => setShowAdvanced((current) => !current)}
      >
        <Icon name={showAdvanced ? "close" : "plus"} size={14} />
        {showAdvanced ? "收起更多设定" : "更多设定"}
      </button>
      {showAdvanced && <div id="role-advanced-fields" className="advanced-fields">
        {characterFields.slice(1).map(renderField)}
      </div>}
    </fieldset>}
    {!workspace && error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
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
    {!workspace && writable && !cropping && <div className="form-actions role-save-actions">
      <button type="button" className="ghost" disabled={busy} onClick={onCancel}>取消</button>
      <button type="submit" className="primary" disabled={busy}>{saving ? "保存中…" : "保存"}</button>
    </div>}
    {writable && !cropping && selected && !creating && <section className="role-delete-section" aria-label="角色管理">
      <div><h3>删除角色</h3><p className="muted">删除后无法恢复角色、聊天记录和记忆。</p></div>
      <button ref={deleteButton} type="button" className="danger" disabled={deleting || busy} onClick={() => { setError(""); setConfirmDelete(true); }}><Icon name="trash" size={16} />删除角色</button>
    </section>}
  </form>
    {confirmDelete && selected && <RoleDeleteConfirmation name={selected.name} busy={deleting} error={error} editor={form.current} trigger={deleteButton.current} onCancel={() => { if (!deleting) setConfirmDelete(false); }} onConfirm={onDelete} />}
  </>;
}
