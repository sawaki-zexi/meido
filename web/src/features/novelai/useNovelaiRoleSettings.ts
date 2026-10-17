import { useEffect, useState } from 'react';
import { api, errorMessage } from '../../api/client';
import { defaultNovelaiRoleConfig, novelai, type NovelaiConfiguration, type NovelaiRoleConfig } from '../../api/novelai';
import type { AgentToolConfig, ModelConfiguration } from '../../api/types';
import type { RoleDraft } from '../role-management/draft';

export function useNovelaiRoleSettings(draft: RoleDraft, onChange: (draft: RoleDraft) => void) {
  const [configuration, setConfiguration] = useState<NovelaiConfiguration | null>(null);
  const [models, setModels] = useState<ModelConfiguration[]>([]);
  const [modelLoading, setModelLoading] = useState(true);
  const [modelsReady, setModelsReady] = useState(false);
  const [error, setError] = useState('');
  const [modelError, setModelError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const config = { ...defaultNovelaiRoleConfig, ...(draft.preservedAgentConfig as AgentToolConfig & { novelai?: Partial<NovelaiRoleConfig> }).novelai };
  useEffect(() => {
    let active = true;
    void novelai.configuration().then((value) => { if (active) setConfiguration(value); })
      .catch((cause) => { if (active) setError(errorMessage(cause, '无法加载 NovelAI 设置')); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    let active = true;
    setModelLoading(true); setModelsReady(false); setModelError('');
    void api<{ configurations: ModelConfiguration[] }>('/api/model/configurations')
      .then((result) => { if (active) { setModels(result.configurations); setModelsReady(true); } })
      .catch((cause) => { if (active) setModelError(errorMessage(cause, '无法加载 CG 辅助连接')); })
      .finally(() => { if (active) setModelLoading(false); });
    return () => { active = false; };
  }, [refresh]);
  const patch = (changes: Partial<NovelaiRoleConfig>) => onChange({ ...draft,
    preservedAgentConfig: { ...draft.preservedAgentConfig, novelai: { ...config, ...changes } } as AgentToolConfig });
  return { configuration, models, modelLoading, error, modelError, config, patch,
    dismissError: () => setError(''), dismissModelError: () => setModelError(''), refreshModels: () => setRefresh((value) => value + 1),
    available: Boolean(configuration?.enabled && configuration.tokenConfigured && !configuration.credentialCleanupPending),
    consent: Boolean(configuration && config.consentRevision === configuration.revision),
    selectedModel: modelsReady ? models.find((model) => model.id === config.scenePromptModelConfigurationId) : undefined };
}
