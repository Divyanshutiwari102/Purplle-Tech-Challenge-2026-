// Tiny API client. The dev server proxies `/api/*` to the FastAPI
// backend; in production the frontend container's nginx does the same.
//
// API base resolution order:
//   1. VITE_API_URL — set by Vercel for production (points at Railway).
//   2. VITE_API_BASE — kept for backward compatibility.
//   3. "/api" — local dev / docker-compose, proxied by nginx.
const BASE =
  (import.meta.env.VITE_API_URL as string | undefined) ??
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  "/api";

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
// Camera streams are scoped per store so the same `CAM_1` slug can
// resolve to different clips for STORE_BLR_002 vs ST1008.
export const cameraSrc = (camId: string, storeId?: string) =>
  storeId
    ? `${BASE}/cameras/stream/${camId}?store_id=${encodeURIComponent(storeId)}`
    : `${BASE}/cameras/stream/${camId}`;
// Single-frame JPEG poster for thumbnails — static so the browser
// doesn't allocate one of its 6 per-host connections to each thumb.
export const posterSrc = (camId: string, storeId?: string) =>
  storeId
    ? `${BASE}/cameras/poster/${camId}?store_id=${encodeURIComponent(storeId)}`
    : `${BASE}/cameras/poster/${camId}`;
export const sseUrl = (storeId: string) => `${BASE}/stores/${storeId}/stream`;
