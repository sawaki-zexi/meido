import { api, request } from "./client";
import type { Role } from "./types";

export type McpRisk = "read_only" | "mutating" | "external";
export type McpServerInput = {
  serverId: string; name: string; command: string[]; cwd: string; env: Record<string, string>;
  roleIds: string[]; enabled: boolean; risk: McpRisk; timeoutSeconds: number; maxOutputChars: number; enableDiscovery: boolean;
};
export type McpServer = Omit<McpServerInput, "env" | "enableDiscovery"> & {
  envKeys: string[]; revision: string; connectionCount: number; activeProcesses: number; errors: string[];
};

export const listMcpServers = () => api<{ servers: McpServer[] }>("/api/mcp/servers");
export const listMcpRoles = () => api<{ roles: Role[] }>("/api/roles");
export const saveMcpServer = (data: McpServerInput, editing: boolean) => api<{ server: McpServer }>(
  editing ? `/api/mcp/servers/${encodeURIComponent(data.serverId)}` : "/api/mcp/servers",
  { method: editing ? "PUT" : "POST", body: JSON.stringify(data) },
);
export const deleteMcpServer = (id: string) => request(`/api/mcp/servers/${encodeURIComponent(id)}`, { method: "DELETE" });
