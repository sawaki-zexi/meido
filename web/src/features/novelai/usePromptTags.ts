import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { uploadChatAttachment } from '../../api/chat';
import { novelai, type PromptTag, type PromptTagInput, type PromptTagPage, type PromptTagPreview } from '../../api/novelai';

const emptyTag = (): PromptTagInput => ({ name: '', category: 'composition', keywords: [], prompt: '', negative_prompt: '', rating: 'general', enabled: true, reference_attachment_id: null });
export function usePromptTags(roleId: string, initiallyOpen = false) {
  const [open, setOpen] = useState(initiallyOpen);
  const [page, setPage] = useState<PromptTagPage | null>(null);
  const [draft, setDraft] = useState<PromptTagInput>(emptyTag);
  const [keywords, setKeywords] = useState('');
  const [editing, setEditing] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [text, setText] = useState('');
  const [prompt, setPrompt] = useState('');
  const [negativePrompt, setNegativePrompt] = useState('');
  const [preview, setPreview] = useState<PromptTagPreview | null>(null);
  const scope = useRef({ roleId, active: true });
  const currentRole = useRef(roleId);
  const previewVersion = useRef(0);
  currentRole.current = roleId;
  const current = (owner: typeof scope.current) => owner.active && owner === scope.current && currentRole.current === owner.roleId;
  useEffect(() => {
    const owner = { roleId, active: true }; scope.current = owner;
    setOpen(initiallyOpen); setPage(null); setDraft(emptyTag()); setKeywords(''); setEditing(undefined);
    setBusy(false); setError(''); setText(''); setPrompt(''); setNegativePrompt(''); setPreview(null); previewVersion.current++;
    if (initiallyOpen) void novelai.promptTags(roleId).then((result) => {
      if (current(owner)) setPage(result);
    }).catch((cause) => {
      if (current(owner)) setError(errorMessage(cause, '提示标签操作未完成，请重查标签库'));
    });
    return () => { owner.active = false; };
  }, [roleId, initiallyOpen]);
  const load = async (owner = scope.current, cursor?: string) => {
    const result = await novelai.promptTags(owner.roleId, cursor);
    if (!current(owner)) return;
    setPage((previous) => cursor && previous ? { ...result, tags: [...previous.tags,
      ...result.tags.filter((tag) => !previous.tags.some((old) => old.id === tag.id))] } : result);
  };
  const run = async (action: (owner: typeof scope.current) => Promise<void>) => {
    if (busy) return;
    const owner = scope.current;
    setBusy(true); setError('');
    try { await action(owner); }
    catch (cause) { if (current(owner)) setError(errorMessage(cause, '提示标签操作未完成，请重查标签库')); }
    finally { if (current(owner)) setBusy(false); }
  };
  const invalidatePreview = () => { previewVersion.current++; setPreview(null); };
  const resetDraft = () => { setDraft(emptyTag()); setKeywords(''); setEditing(undefined); };
  const save = () => run(async (owner) => {
    const words = keywords.split(/\r?\n/).map((word) => word.trim()).filter(Boolean);
    if (!draft.name.trim() || draft.name.length > 80 || !draft.category.trim() || draft.category.length > 80
      || words.length < 1 || words.length > 16 || words.some((word) => word.length > 80))
      throw new Error('标签名称和分类各最多 80 字符；请填写 1–16 个关键词，每个最多 80 字符。');
    if ([draft.prompt, draft.negative_prompt].some((value) => value.length > 4000 || /[^\x20-\x7e]/.test(value)))
      throw new Error('正负 tags 各最多 4000 字符，只能使用英文 ASCII 且不能包含控制符。');
    await novelai.savePromptTag(owner.roleId, { ...draft, name: draft.name.trim(), category: draft.category.trim(), keywords: words }, editing);
    if (!current(owner)) return;
    resetDraft(); invalidatePreview(); await load(owner);
  });
  const uploadReference = (file: File) => run(async (owner) => {
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type))
      throw new Error('参考图只支持 PNG、JPEG 或 WebP。');
    const uploaded = await uploadChatAttachment(owner.roleId, file);
    if (!current(owner)) return;
    if (uploaded.kind !== 'image') throw new Error('上传的文件不是图片。');
    setDraft((previous) => ({ ...previous, reference_attachment_id: uploaded.id }));
    invalidatePreview();
  });
  const remove = (tag: PromptTag) => run(async (owner) => {
    await novelai.deletePromptTag(owner.roleId, tag.id);
    if (!current(owner)) return;
    if (editing === tag.id) resetDraft();
    invalidatePreview(); await load(owner);
  });
  const inspect = () => run(async (owner) => {
    if (text.length > 12000 || [prompt, negativePrompt].some((value) => value.length > 4000 || /[^\x20-\x7e]/.test(value)))
      throw new Error('预览输入最多 12000 字符，基础 tags 各最多 4000 字符且只能使用英文 ASCII。');
    const version = ++previewVersion.current;
    setPreview(null);
    const result = await novelai.previewPromptTags(owner.roleId, text, prompt, negativePrompt);
    if (current(owner) && version === previewVersion.current) setPreview(result);
  });
  return { open, page, draft, keywords, editing, busy, error, text, prompt, negativePrompt, preview,
    dismissError: () => setError(''), toggle: () => { if (open) setOpen(false); else { setOpen(true); void run((owner) => load(owner)); } },
    reload: () => run((owner) => load(owner)), more: () => run((owner) => load(owner, page?.nextCursor ?? undefined)),
    patch: (changes: Partial<PromptTagInput>) => setDraft({ ...draft, ...changes }), setKeywords,
    edit: (tag: PromptTag) => {
      const { id, referenceAttachment: _referenceAttachment, ...input } = tag;
      setDraft({ ...emptyTag(), ...input, category: input.category || 'composition', reference_attachment_id: input.reference_attachment_id ?? null });
      setEditing(id); setKeywords(input.keywords.join('\n'));
    },
    uploadReference,
    resetDraft, save, remove, inspect,
    setText: (value: string) => { setText(value); invalidatePreview(); },
    setPrompt: (value: string) => { setPrompt(value); invalidatePreview(); },
    setNegativePrompt: (value: string) => { setNegativePrompt(value); invalidatePreview(); } };
}
