import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { autonomy, DriftAdoptionError, type AutonomyConfiguration, type DriftPreview, type DriftSkill } from '../../api/autonomy';

export function useDriftSkills(roleId: string, config: AutonomyConfiguration, hasUnsavedChanges: boolean) {
  const [open, setOpen] = useState(false);
  const [skills, setSkills] = useState<DriftSkill[] | null>(null);
  const [skillId, setSkillId] = useState('');
  const [preview, setPreview] = useState<DriftPreview | null>(null);
  const [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [pendingAdoption, setPendingAdoption] = useState<DriftPreview | null>(null);
  const generation = useRef(0);
  const currentConfig = useRef(config);
  const currentUnsaved = useRef(hasUnsavedChanges);
  currentConfig.current = config; currentUnsaved.current = hasUnsavedChanges;
  useEffect(() => {
    generation.current++; setOpen(false); setSkills(null); setSkillId(''); setPreview(null); setTrusted(false);
    setBusy(false); setError(''); setMessage(''); setPendingAdoption(null);
    return () => { generation.current++; };
  }, [roleId, config.revision, config.driftAuthoring.scopeId, config.driftAuthoring.revision]);
  useEffect(() => { if (hasUnsavedChanges) { generation.current++; setPreview(null); setTrusted(false); setBusy(false); setPendingAdoption(null); } }, [hasUnsavedChanges]);
  const matches = (item: DriftPreview) => !currentUnsaved.current && item.skillId === skillId.trim()
    && item.configRevision === currentConfig.current.revision && item.scopeId === currentConfig.current.driftAuthoring.scopeId
    && item.scopeRevision === currentConfig.current.driftAuthoring.revision && currentConfig.current.driftAuthoring.enabled
    && item.status !== 'invalid' && Boolean(item.previewId && item.bundleSha256);
  const run = async (action: (version: number) => Promise<void>) => {
    if (busy) return;
    const version = ++generation.current;
    setBusy(true); setError(''); setMessage('');
    try { await action(version); }
    catch (cause) { if (version === generation.current) { setTrusted(false); setError(errorMessage(cause, 'Drift 技能操作尚未确认，请重读状态')); } }
    finally { if (version === generation.current) setBusy(false); }
  };
  const reload = () => run(async (version) => {
    setPreview(null); setTrusted(false); setPendingAdoption(null);
    const result = await autonomy.driftSkills(roleId);
    if (version === generation.current) setSkills(result.skills);
  });
  const inspect = () => run(async (version) => {
    setPreview(null); setTrusted(false); setPendingAdoption(null);
    const result = await autonomy.previewDriftArtifact(roleId, skillId.trim());
    if (version === generation.current && !currentUnsaved.current) setPreview(result);
  });
  const adopt = () => {
    const current = pendingAdoption ?? preview;
    if (!current || !trusted || !matches(current)) return;
    return run(async (version) => {
      setPendingAdoption(current);
      let result;
      try { result = await autonomy.adoptDriftSkill(roleId, current); }
      catch (cause) {
        if (version === generation.current && cause instanceof DriftAdoptionError && [400, 404, 409, 422].includes(cause.status)) {
          setPreview(null); setPendingAdoption(null);
        }
        throw cause;
      }
      if (version !== generation.current || !matches(current)) return;
      setSkills(result.skills); setTrusted(false); setPreview(null); setPendingAdoption(null);
      setMessage('已登记当前精确产物；执行是否就绪以技能状态与当前许可为准。');
    });
  };
  return { open, skills, skillId, preview, trusted, busy, error, message, pendingAdoption,
    canAdopt: Boolean(preview && trusted && matches(preview)),
    toggle: () => { setOpen(!open); if (!open) void reload(); }, reload, inspect, adopt,
    changeSkillId: (value: string) => { generation.current++; setSkillId(value); setPreview(null); setTrusted(false); setBusy(false); setMessage(''); setPendingAdoption(null); },
    setTrusted, dismissError: () => setError(''), dismissMessage: () => setMessage('') };
}
