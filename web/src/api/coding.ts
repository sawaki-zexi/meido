import { api } from "./client";

export type CodingRoleConfig = { enabled: boolean; repoGrantIds: string[]; profileIds: string[]; defaultHerdrProfileId: string | null; defaultCodexProfileId: string | null; maxAttemptsPerDay: number };
export const defaultCodingRole = (): CodingRoleConfig => ({ enabled: false, repoGrantIds: [], profileIds: [], defaultHerdrProfileId: null, defaultCodexProfileId: null, maxAttemptsPerDay: 5 });
export type CodingRepository = { id: string; root: string; allowedRoleIds: string[]; baseRef: string; remote: string; validation: { id: string; argv: string[]; cwd: string }[]; revision: string };
export type CodingProfile = { id: string; name: string; kind: "codex" | "claude"; executor: "herdr" | "codex_exec"; executable: string; sha256: string; version: string; model: string; authDirectory: string; allowedRoleIds: string[]; configurationManifest: unknown[]; revision: string; verification: { status: string; reason?: string } };
export type CodingConfiguration = { enabled: boolean; repositories: CodingRepository[]; profiles: CodingProfile[]; nativeBuild: { executable: string; sha256: string; version: string } | null };
export type CodingTask = { id: string; attemptId: string; status: string; runtimeStatus: string; cleanupState: string; deliveryStatus: string; summary?: string; nativeSession?: string; branch?: string; blockers?: string[]; validation?: { id: string; status: string }[] };
export const getCodingConfiguration = () => api<CodingConfiguration>("/api/coding/configuration");
export const setCodingEnabled = (enabled: boolean) => api<CodingConfiguration>("/api/coding/configuration", { method: "PUT", body: JSON.stringify({ enabled }) });
export const saveCodingRepository = (value: Omit<CodingRepository, "id" | "revision"> & { id?: string }) => api<CodingRepository>("/api/coding/repositories", { method: "POST", body: JSON.stringify(value) });
export const saveCodingProfile = (value: Omit<CodingProfile, "id" | "revision" | "verification"> & { id?: string }) => api<CodingProfile>("/api/coding/profiles", { method: "POST", body: JSON.stringify(value) });
export const saveCodingBuild = (executable: string, sha256: string) => api("/api/coding/native-build", { method: "PUT", body: JSON.stringify({ executable, sha256 }) });
export const listCodingTasks = (roleId: string) => api<{ tasks: CodingTask[]; availability: { status: string; reason: string } }>(`/api/coding/roles/${encodeURIComponent(roleId)}/tasks`);
export const cancelCodingTask = (roleId: string, taskId: string) => api<CodingTask>(`/api/coding/roles/${encodeURIComponent(roleId)}/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
export type CodingProbe = { id: string; profileId: string; status: string; reason: string; cleanupState?: string };
export const listCodingProbes = () => api<{ probes: CodingProbe[] }>("/api/coding/probes");
export const startCodingProbe = (profile: CodingProfile, roleId: string, buildSha256: string, requestId: string) => api<CodingProbe>(`/api/coding/profiles/${encodeURIComponent(profile.id)}/probe`, { method: "POST", body: JSON.stringify({ revision: profile.revision, roleId, buildSha256, approveNativeTurns: 2, requestId }) });
