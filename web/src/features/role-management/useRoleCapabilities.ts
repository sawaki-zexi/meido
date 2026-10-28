import { useCallback, useEffect, useRef, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role, TodoReminderDay, TodoReminderRole, TodoSettings } from '../../api/types';
import { proactive, type ProactiveState } from '../../api/proactive';
import { desktopPet, type PetState } from '../../api/desktopPet';

export type RuntimeCapability = 'natural' | 'autonomy' | 'pet' | 'novelai' | 'reminders';
const names = { natural: '自然聊天', autonomy: '主动推送', pet: '桌宠', novelai: '自动场景 CG', reminders: '待办提醒' };
type ReminderState = { settings: TodoSettings; role: TodoReminderRole; today: TodoReminderDay };
// A chat-scheduled exit may still leave this role eligible for today's reminder.
const participatesInReminders = (role?: TodoReminderRole) => Boolean(role?.enabled || role?.activeToday);
async function getReminders(roleId: string): Promise<ReminderState> {
  const [settings, roles, today] = await Promise.all([
    api<TodoSettings>('/api/todos/settings'), api<{ roles: TodoReminderRole[] }>('/api/todos/reminder-roles'),
    api<TodoReminderDay>('/api/todos/reminders/today'),
  ]);
  const role = roles.roles.find(item => item.id === roleId);
  if (!role) throw new Error('当前角色不在提醒角色列表中，请刷新后重试');
  return { settings, role, today };
}

export function useRoleCapabilities(roleId: string, onRoleSaved?: (role: Role) => void, settingsVersion = 0) {
  const [push, setPush] = useState<ProactiveState | null>(null);
  const [pet, setPet] = useState<PetState | null>(null);
  const [sceneRole, setSceneRole] = useState<Role | null>(null);
  const [reminders, setReminders] = useState<ReminderState | null>(null);
  const [draft, setDraft] = useState({ natural: false, autonomy: false, pet: false, novelai: false, reminders: false });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loadErrors, setLoadErrors] = useState<Partial<Record<RuntimeCapability, string>>>({});
  const generation = useRef(0);
  const saved = { natural: sceneRole?.agentConfig?.naturalChatEnabled === true, autonomy: push?.settings.enabled ?? false, pet: pet?.enabled ?? false, novelai: sceneRole?.agentConfig?.novelai?.sceneCgEnabled === true, reminders: participatesInReminders(reminders?.role) };
  const snapshot = useRef({ saved, draft });
  snapshot.current = { saved, draft };
  const load = useCallback(async (only?: RuntimeCapability, preserveDraft = false) => {
    const request = ++generation.current;
    const before = snapshot.current;
    setBusy(true); setError(''); setNotice('');
    const keys = only ? [only] : ['natural', 'autonomy', 'pet', 'novelai', 'reminders'] as const;
    // Both role-owned switches use one current role record, not separate copies.
    const roleRequest = keys.some(key => key === 'natural' || key === 'novelai')
      ? api<{ role: Role }>(`/api/roles/${encodeURIComponent(roleId)}`) : null;
    const results = await Promise.allSettled(keys.map(async key => {
      if (key === 'autonomy') return { key, value: await proactive.get(roleId) } as const;
      if (key === 'pet') return { key, value: await desktopPet.status(roleId) } as const;
      if (key === 'reminders') return { key, value: await getReminders(roleId) } as const;
      return { key, value: (await roleRequest!).role } as const;
    }));
    if (request !== generation.current) return;
    results.forEach((result, index) => {
      const key = keys[index];
      if (result.status === 'rejected') {
        setLoadErrors(current => ({ ...current, [key]: errorMessage(result.reason, `无法读取${names[key]}`) }));
        return;
      }
      const data = result.value;
      setLoadErrors(current => ({ ...current, [key]: undefined }));
      let enabled: boolean;
      if (data.key === 'autonomy') { setPush(data.value); enabled = data.value.settings.enabled; }
      else if (data.key === 'pet') { setPet(data.value); enabled = data.value.enabled; }
      else if (data.key === 'reminders') { setReminders(data.value); enabled = participatesInReminders(data.value.role); }
      else {
        setSceneRole(data.value);
        setDraft(current => ({ ...current,
          natural: (preserveDraft || data.key !== 'natural') && before.draft.natural !== before.saved.natural
            ? current.natural : data.value.agentConfig?.naturalChatEnabled === true,
          novelai: (preserveDraft || data.key !== 'novelai') && before.draft.novelai !== before.saved.novelai
            ? current.novelai : data.value.agentConfig?.novelai?.sceneCgEnabled === true,
        }));
        return;
      }
      setDraft(current => ({ ...current, [key]: preserveDraft && before.draft[key] !== before.saved[key] ? current[key] : enabled }));
    });
    setBusy(false);
  }, [roleId]);
  useEffect(() => { void load(undefined, true); return () => { ++generation.current; }; }, [load, settingsVersion]);
  const dirty = (Object.keys(saved) as RuntimeCapability[]).some(key => draft[key] !== saved[key]);
  function change(key: RuntimeCapability, enabled: boolean) { setDraft(current => ({ ...current, [key]: enabled })); setNotice(''); }
  function receiveScene(role: Role) {
    const preserveNatural = snapshot.current.draft.natural !== snapshot.current.saved.natural;
    setSceneRole(role); setDraft(current => ({ ...current, novelai: role.agentConfig?.novelai?.sceneCgEnabled === true,
      natural: preserveNatural ? current.natural : role.agentConfig?.naturalChatEnabled === true }));
    onRoleSaved?.(role);
  }
  async function save() {
    if (busy || !dirty) return;
    const request = ++generation.current;
    setBusy(true); setError(''); setNotice('');
    let savedCount = 0;
    try {
      if (push && draft.autonomy !== saved.autonomy) {
        const value = await proactive.save(roleId, { ...push.settings, enabled: draft.autonomy });
        if (request !== generation.current) return;
        setPush(value); setDraft(current => ({ ...current, autonomy: value.settings.enabled })); ++savedCount;
      }
      if (pet && draft.pet !== saved.pet) {
        const value = await desktopPet.configure(roleId, pet, pet.selectedPackageId, draft.pet);
        if (request !== generation.current) return;
        setPet(value); setDraft(current => ({ ...current, pet: value.enabled })); ++savedCount;
      }
      if (reminders && draft.reminders !== saved.reminders) {
        const value = await api<{ roleId: string; enabled: boolean; effectiveDate: string }>(`/api/todos/reminder-roles/${encodeURIComponent(roleId)}`, {
          method: 'PUT', body: JSON.stringify({ enabled: draft.reminders }),
        });
        if (request !== generation.current) return;
        setReminders({ ...reminders, role: { ...reminders.role, enabled: value.enabled, effectiveDate: value.effectiveDate,
          activeToday: value.enabled ? reminders.role.activeToday : false } });
        setDraft(current => ({ ...current, reminders: value.enabled })); ++savedCount;
      }
      // Enabling CG still goes through its model/settings form. Patch only
      // changed role fields so unrelated settings and a fresh profile survive.
      if (sceneRole && (draft.natural !== saved.natural || saved.novelai && !draft.novelai)) {
        const path = `/api/roles/${encodeURIComponent(roleId)}`;
        const { role: fresh } = await api<{ role: Role }>(path);
        if (request !== generation.current) return;
        const { role: updated } = await api<{ role: Role }>(path, { method: 'PUT', body: JSON.stringify({
          name: fresh.name, description: fresh.description, profile: fresh.profile,
          agentConfig: {
            ...(draft.natural !== saved.natural ? { naturalChatEnabled: draft.natural } : {}),
            ...(saved.novelai && !draft.novelai ? { novelai: { sceneCgEnabled: false, autoWritebackRoleAssets: false, consentRevision: 0 } } : {}),
          },
        }) });
        if (request !== generation.current) return;
        setSceneRole(updated); setDraft(current => ({ ...current,
          natural: updated.agentConfig?.naturalChatEnabled === true,
          novelai: updated.agentConfig?.novelai?.sceneCgEnabled === true }));
        onRoleSaved?.(updated); ++savedCount;
      }
      setNotice('已保存');
    } catch (cause) {
      if (request === generation.current) setError(`${savedCount ? '部分更改已保存；' : ''}${errorMessage(cause, '无法保存能力设置')}`);
    } finally { if (request === generation.current) setBusy(false); }
  }
  return { push, pet, sceneRole, reminders, draft, saved, busy, dirty, error, notice, loadErrors, load, change, receiveScene,
    save, reset: () => { setDraft(saved); setError(''); setNotice(''); }, dismissError: () => setError('') };
}
