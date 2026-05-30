// Tiny API client. The dev server proxies `/api/*` to the FastAPI
// backend; in production the frontend container's nginx does the same.
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api";

export async function jget<T = any>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json() as Promise<T>;
}

export async function jpost<T = any>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json() as Promise<T>;
}

export const apiBase = BASE;
export const cameraSrc = (camId: string) => `${BASE}/cameras/stream/${camId}`;
export const sseUrl = (storeId: string) => `${BASE}/stores/${storeId}/stream`;
