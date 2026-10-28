import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { uploadChatAttachment } from '../../api/chat';
import { novelai, type PromptTag, type PromptTagInput, type PromptTagPage, type PromptTagPreview, type PromptTagRating } from '../../api/novelai';

const emptyTag = (): PromptTagInput => ({ name: '', category: 'composition', keywords: [], prompt: '', negative_prompt: '', rating: 'general', enabled: true, reference_attachment_id: null });
const normalizeSearch = (value: string) => value.normalize('NFKC').toLowerCase();
type TagFilters = { query: string; category: string; rating: '' | PromptTagRating; status: '' | 'enabled' | 'disabled' };
const emptyFilters = (): TagFilters => ({ query: '', category: '', rating: '', status: '' });
export function usePromptTags(roleId: string, initiallyOpen = false) {
  const [open, setOpen] = useState(initiallyOpen);
  const [page, setPage] = useState<PromptTagPage | null>(null);
  const [draft, setDraft] = useState<PromptTagInput>(emptyTag);
  const [keywords, setKeywords] = useState('');
  const [editing, setEditing] = useState<string>();
  const [editorOpen, setEditorOpen] = useState(false);
  const [original, setOriginal] = useState<PromptTagInput>(emptyTag);
  const [pendingDelete, setPendingDelete] = useState<PromptTag | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [text, setText] = useState('');
  const [prompt, setPrompt] = useState('');
  const [negativePrompt, setNegativePrompt] = useState('');
  const [preview, setPreview] = useState<PromptTagPreview | null>(null);
  const [filters, setFilters] = useState<TagFilters>(emptyFilters);
  const scope = useRef({ roleId, active: true });
  const currentRole = useRef(roleId);
  const previewVersion = useRef(0);
  currentRole.current = roleId;
  const current = (owner: typeof scope.current) => owner.active && owner === scope.current && currentRole.current === owner.roleId;
  const load = async (owner = scope.current) => {
    setPage(null);
    let result = await novelai.promptTags(owner.roleId);
    if (!current(owner)) return;
    const revision = result.revision;
    const entries = [...result.tags];
    const cursors = new Set<string>();
    while (result.nextCursor) {
      if (cursors.has(result.nextCursor) || entries.length >= 250)
        throw new Error('标签库未能完整加载，请刷新后再查找。');
      cursors.add(result.nextCursor);
      result = await novelai.promptTags(owner.roleId, result.nextCursor);
      if (!current(owner)) return;
      if (result.revision !== revision) throw new Error('标签库已更新，请刷新后再查找。');
      entries.push(...result.tags);
    }
    if (entries.length > 250 || new Set(entries.map((tag) => tag.id)).size !== entries.length)
      throw new Error('标签库未能完整加载，请刷新后再查找。');
    setPage({ tags: entries, revision, nextCursor: null });
  };
  useEffect(() => {
    const owner = { roleId, active: true }; scope.current = owner;
    setOpen(initiallyOpen); setPage(null); setDraft(emptyTag()); setKeywords(''); setEditing(undefined);
    setEditorOpen(false); setOriginal(emptyTag()); setPendingDelete(null); setSaved(false);
    setBusy(initiallyOpen); setError(''); setText(''); setPrompt(''); setNegativePrompt(''); setPreview(null); setFilters(emptyFilters()); previewVersion.current++;
    if (initiallyOpen) void load(owner).catch((cause) => {
      if (current(owner)) setError(errorMessage(cause, '提示标签操作未完成，请重查标签库'));
    }).finally(() => {
      if (current(owner)) setBusy(false);
    });
    return () => { owner.active = false; };
  }, [roleId, initiallyOpen]);
  const run = async (action: (owner: typeof scope.current) => Promise<void>) => {
    if (busy) return;
    const owner = scope.current;
    setBusy(true); setError('');
    try { await action(owner); }
    catch (cause) { if (current(owner)) setError(errorMessage(cause, '提示标签操作未完成，请重查标签库')); }
    finally { if (current(owner)) setBusy(false); }
  };
  const invalidatePreview = () => { previewVersion.current++; setPreview(null); };
  const resetDraft = () => { setDraft(original); setKeywords(original.keywords.join('\n')); setSaved(false); };
  const openEditor = (tag?: PromptTag) => {
    const { id, referenceAttachment: _referenceAttachment, ...input } = tag ?? { ...emptyTag(), id: undefined, referenceAttachment: null };
    const value = { ...emptyTag(), ...input, category: input.category || 'composition', reference_attachment_id: input.reference_attachment_id ?? null };
    setOriginal(value); setDraft(value); setEditing(id); setKeywords(value.keywords.join('\n')); setEditorOpen(true); setSaved(false);
  };
  const dirty = JSON.stringify(draft) !== JSON.stringify(original) || keywords !== original.keywords.join('\n');
  const save = () => run(async (owner) => {
    if (!dirty) return;
    const words = keywords.split(/\r?\n/).map((word) => word.trim()).filter(Boolean);
    if (!draft.name.trim() || draft.name.length > 80 || !draft.category.trim() || draft.category.length > 80
      || words.length < 1 || words.length > 16 || words.some((word) => word.length > 80))
      throw new Error('标签名称和分类各最多 80 字符；请填写 1–16 个关键词，每个最多 80 字符。');
    if ([draft.prompt, draft.negative_prompt].some((value) => value.length > 4000 || /[^\x20-\x7e]/.test(value)))
      throw new Error('正负 tags 各最多 4000 字符，只能使用英文 ASCII 且不能包含控制符。');
    const result = await novelai.savePromptTag(owner.roleId, { ...draft, name: draft.name.trim(), category: draft.category.trim(), keywords: words }, editing);
    if (!current(owner)) return;
    openEditor(result.tag); setSaved(true); invalidatePreview(); await load(owner);
  });
  const uploadReference = (file: File) => run(async (owner) => {
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type))
      throw new Error('参考图只支持 PNG、JPEG 或 WebP。');
    const uploaded = await uploadChatAttachment(owner.roleId, file);
    if (!current(owner)) return;
    if (uploaded.kind !== 'image') throw new Error('上传的文件不是图片。');
    setDraft((previous) => ({ ...previous, reference_attachment_id: uploaded.id }));
    setSaved(false);
    invalidatePreview();
  });
  const remove = (tag: PromptTag) => run(async (owner) => {
    await novelai.deletePromptTag(owner.roleId, tag.id);
    if (!current(owner)) return;
    setPendingDelete(null);
    if (editing === tag.id) { openEditor(); setEditorOpen(false); }
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
  const search = normalizeSearch(filters.query.trim());
  const filteredTags = page?.tags.filter((tag) => (!filters.category || tag.category === filters.category)
    && (!filters.rating || tag.rating === filters.rating)
    && (!filters.status || tag.enabled === (filters.status === 'enabled'))
    && [tag.name, ...tag.keywords, tag.prompt, tag.negative_prompt].some((value) => normalizeSearch(value).includes(search))) ?? [];
  const categoryValues = new Set(page?.tags.map((tag) => tag.category) ?? []);
  if (filters.category) categoryValues.add(filters.category);
  const categories = [...categoryValues].sort((a, b) => a.localeCompare(b));
  return { open, page, draft, keywords, editing, editorOpen, dirty, saved, pendingDelete, busy, error, text, prompt, negativePrompt, preview, filters, filteredTags, categories,
    create: () => openEditor(), back: () => setEditorOpen(false), resume: () => setEditorOpen(true),
    requestDelete: (tag: PromptTag) => { setError(''); setPendingDelete(tag); }, cancelDelete: () => { if (!busy) setPendingDelete(null); },
    filter: (changes: Partial<TagFilters>) => setFilters((previous) => ({ ...previous, ...changes })),
    clearFilters: () => setFilters(emptyFilters()),
    dismissError: () => setError(''), toggle: () => { if (open) setOpen(false); else { setOpen(true); void run((owner) => load(owner)); } },
    reload: () => run((owner) => load(owner)),
    patch: (changes: Partial<PromptTagInput>) => { setDraft({ ...draft, ...changes }); setSaved(false); },
    setKeywords: (value: string) => { setKeywords(value); setSaved(false); },
    edit: (tag: PromptTag) => { if (tag.id === editing) setEditorOpen(true); else openEditor(tag); },
    uploadReference,
    resetDraft, save, remove, inspect,
    setText: (value: string) => { setText(value); invalidatePreview(); },
    setPrompt: (value: string) => { setPrompt(value); invalidatePreview(); },
    setNegativePrompt: (value: string) => { setNegativePrompt(value); invalidatePreview(); } };
}
