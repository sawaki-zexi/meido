import { vi } from "vitest";
import type { Message, Role } from "../api/types";

type Reply = { status?: number; body?: unknown; stream?: string[] | ReadableStream<Uint8Array>; headers?: Record<string, string> };
type Handler = (init: RequestInit | undefined, match: RegExpMatchArray) => Reply | Promise<Reply>;

export const role = (overrides: Partial<Role> & { id: string; name: string }): Role => ({
  description: "",
  profile: { profile: "设定", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" },
  createdAt: "2026-09-30T00:00:00Z",
  updatedAt: "2026-09-30T00:00:00Z",
  ...overrides,
});

export const message = (overrides: Partial<Message> & { id: string; role: Message["role"]; content: string }): Message => ({
  sessionKey: "role:r1", sequence: 1, status: "completed", createdAt: "2026-09-30T00:00:00Z", ...overrides,
});

export const sse = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

/** Routes `fetch` calls by "METHOD path" regex; unmatched requests fail loudly. */
export function fakeBackend(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body?: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    let body: unknown;
    if (typeof init?.body === "string") {
      try { body = JSON.parse(init.body); } catch { body = init.body; }
    } else body = init?.body;
    calls.push({ method, path, body });
    for (const [pattern, handler] of Object.entries(routes)) {
      const match = `${method} ${path}`.match(new RegExp(`^${pattern}$`));
      if (!match) continue;
      const reply = await handler(init, match);
      if (reply.stream) {
        const encoder = new TextEncoder();
        const chunks = reply.stream;
        const body = chunks instanceof ReadableStream ? chunks : new ReadableStream<Uint8Array>({ start(controller) { chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk))); controller.close(); } });
        return new Response(body, { status: 200, headers: { "content-type": "text/event-stream", ...reply.headers } });
      }
      const status = reply.status ?? 200;
      return new Response(status === 204 ? null : JSON.stringify(reply.body ?? {}), { status, headers: { "content-type": "application/json" } });
    }
    if (method === "GET" && path === "/api/todos/settings") return new Response(JSON.stringify({ enabled: false, remindersEnabled: true, timezone: "Asia/Shanghai" }), { status: 200, headers: { "content-type": "application/json" } });
    if (method === "GET" && path === "/api/coding/configuration") return new Response(JSON.stringify({ enabled: false, repositories: [], profiles: [], nativeBuild: null }), { status: 200, headers: { "content-type": "application/json" } });
    if (method === "GET" && path === "/api/coding/probes") return new Response(JSON.stringify({ probes: [] }), { status: 200, headers: { "content-type": "application/json" } });
    if (method === "GET" && path === "/api/coding/publication") return new Response(JSON.stringify({ revision: 1, configured: false, cleanupPending: false }), { status: 200, headers: { "content-type": "application/json" } });
    if (method === "GET" && path === "/api/todos/reminders") return new Response(JSON.stringify({ reminders: [], unreadCount: 0 }), { status: 200, headers: { "content-type": "application/json" } });
    if (method === "GET" && path === "/api/services/novelai") return new Response(JSON.stringify({ enabled: false, tokenConfigured: false, credentialCleanupPending: false, revision: 1, allowedModels: ['nai-diffusion-4-5-curated'], defaultModel: 'nai-diffusion-4-5-curated', allowedSamplers: ['k_euler_ancestral', 'k_euler'], defaultSampler: 'k_euler_ancestral', maxPixels: 1048576, maxSteps: 28, dailyAttemptLimit: 20 }), { status: 200, headers: { 'content-type': 'application/json' } });
    throw new Error(`Unexpected request: ${method} ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls };
}
