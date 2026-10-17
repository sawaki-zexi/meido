import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { Notice } from "../../ui/Status";

type Preview = {
  manifestPath: string; manifestSha256: string; manifestReady: boolean; preparedManifest: string;
  files: { path: string; size: number; sha256: string }[]; sourceGrants: Record<string, unknown>[];
};

type Props = {
  roleId: string; importManifest: (path: string, sha: string) => void;
  importSources: (sources: Record<string, unknown>[]) => boolean;
};

function useSourceArtifactPreview({ roleId, importManifest, importSources }: Props) {
  const [relativePath, setRelativePath] = useState("");
  const [serverId, setServerId] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const generation = useRef(0);
  useEffect(() => { return () => { ++generation.current; }; }, [roleId]);

  function change(path: string, server: string) {
    ++generation.current;
    setRelativePath(path); setServerId(server); setPreview(null); setError(""); setMessage(""); setBusy(false);
  }

  async function inspect() {
    const current = ++generation.current;
    setBusy(true); setPreview(null); setError(""); setMessage("");
    try {
      const value = await api<Preview>(`/api/roles/${encodeURIComponent(roleId)}/autonomy/source-artifact-preview`, {
        method: "POST", body: JSON.stringify({ manifestPath: relativePath, ...(serverId.trim() ? { serverId: serverId.trim() } : {}) }),
      });
      if (current === generation.current) setPreview(value);
    } catch (cause) {
      if (current === generation.current) setError(errorMessage(cause, "产物无法预览，请检查当前角色文件和已保存服务"));
    } finally { if (current === generation.current) setBusy(false); }
  }

  async function copyManifest() {
    if (!preview) return;
    const current = generation.current;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(preview.preparedManifest);
      if (current === generation.current) setMessage("已复制清单，请保存到原角色路径后重新预览。");
    } catch {
      if (current === generation.current) setMessage("无法自动复制，请手动复制修正后的清单。");
    }
  }

  function applyManifest() {
    if (preview?.manifestReady) importManifest(preview.manifestPath, preview.manifestSha256);
  }

  function applySources() {
    if (preview?.manifestReady && importSources(preview.sourceGrants)) {
      setMessage("来源草稿已填入，默认关闭；检查每项许可并显式保存。未执行任何来源。");
    }
  }

  return { relativePath, serverId, preview, busy, error, message, dismissError: () => setError(""), change, inspect, copyManifest, applyManifest, applySources };
}

export function SourceArtifactPreview(props: Props) {
  const { relativePath, serverId, preview, busy, error, message, dismissError, change, inspect, copyManifest, applyManifest, applySources } = useSourceArtifactPreview(props);

  return <fieldset aria-label="来源产物预览">
    <legend>来源产物预览</legend>
    <p className="field-hint">先用 $create-proactive-source 创建角色文件。此处只读计算 hash，不执行、注册或授权。修正清单需由角色文件工具或你保存后再次预览；服务命令与凭据仍在 MCP 设置中明确配置。</p>
    <label className="field"><span>产物相对清单路径</span><input aria-label="产物相对清单路径" value={relativePath}
      placeholder="mcp/example/manifest.json" onChange={(event) => change(event.target.value, serverId)} /></label>
    <label className="field"><span>已保存的 MCP 服务 ID</span><input aria-label="已保存的 MCP 服务 ID" value={serverId}
      onChange={(event) => change(relativePath, event.target.value)} /><small>可留空先检查文件；填入主人已保存并授权当前角色的服务，才能准备来源许可草稿。</small></label>
    <button type="button" disabled={busy || !relativePath.trim()} onClick={() => void inspect()}>预览来源产物</button>
    {error && <Notice onDismiss={dismissError}>{error}</Notice>}{message && <p role="status">{message}</p>}
    {preview && <>
      <p>{preview.manifestReady ? "清单与实际文件 hash 一致；尚未信任或验证代码。" : "清单 hash 尚未匹配；保存下面的修正清单后重新预览。"}</p>
      <div aria-label="产物文件及 SHA256">{preview.files.map((file) => <p key={file.path}>{file.path} · {file.size} bytes · {file.sha256}</p>)}</div>
      <label className="field"><span>修正后的清单</span><textarea aria-label="修正后的清单" rows={7} readOnly value={preview.preparedManifest} /></label>
      <button type="button" onClick={() => void copyManifest()}>复制修正清单</button>
      <p>当前清单 SHA256：{preview.manifestSha256}</p>
      <button type="button" disabled={!preview.manifestReady} onClick={applyManifest}>填入验证清单</button>
      {preview.sourceGrants.length > 0 && <button type="button" disabled={!preview.manifestReady} onClick={applySources}>填入来源许可草稿</button>}
    </>}
  </fieldset>;
}
