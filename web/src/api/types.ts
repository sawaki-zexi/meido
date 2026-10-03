export type RoleProfile = { profile: string; personality: string; behaviorRules: string; responseConstraints: string; nickname: string };
export type Role = { id: string; name: string; description: string; profile: RoleProfile; createdAt: string; updatedAt: string; avatarUrl?: string | null; avatarMediaType?: string | null; avatarOriginalUrl?: string | null; avatarOriginalMediaType?: string | null; cardImageUrl?: string | null; cardImageMediaType?: string | null };
export type MessageStatus = "streaming" | "completed" | "failed";
export type Message = { id: string; sessionKey: string; sequence: number; role: "user" | "assistant"; content: string; status: MessageStatus; createdAt: string };
export type Session = { sessionKey: string; roleId: string; createdAt: string; updatedAt: string };
export type Provider = { id: string; label: string; provider: string; baseUrl: string; modelHint: string };
export type ModelConfiguration = { id: string; providerId: string; provider: string; baseUrl: string; model: string; apiKeyConfigured: boolean; active?: boolean };
