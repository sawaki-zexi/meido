/** Turns a FastAPI error body into a readable message. */
export function errorDetail(data: { detail?: unknown; error?: unknown }, status: number): string {
  if (Array.isArray(data.detail)) {
    const joined = data.detail.map((item: { msg?: string }) => item?.msg).filter(Boolean).join("；");
    if (joined) return joined;
  }
  if (typeof data.detail === "string" && data.detail) return data.detail;
  if (typeof data.error === "string" && data.error) return data.error;
  return `请求失败 (${status})`;
}

export async function request(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  } catch {
    throw new Error("无法连接本地后端，请确认服务正在运行");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(errorDetail(data, response.status));
  }
  return response;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init);
  return await response.json().catch(() => ({})) as T;
}

export const errorMessage = (cause: unknown, fallback: string) => cause instanceof Error && cause.message ? cause.message : fallback;
