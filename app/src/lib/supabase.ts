// Dependency-free Supabase client (REST + Storage over fetch) — mirrors the
// engine's stdlib approach and the Loadopoly-OCR env-var names so the hosted
// StockOpoly app reads/writes the SAME Supabase project. No @supabase/supabase-js
// so the bundle stays lean and the build needs no extra install.

const URL = (import.meta.env.VITE_SUPABASE_URL ?? '').replace(/\/+$/, '');
const KEY = import.meta.env.VITE_SUPABASE_ANON_KEY ?? '';

export const SUPABASE_BUCKET = 'stockopoly-photos';

export function isSupabaseConfigured(): boolean {
  return Boolean(URL && KEY);
}

export function supabaseUrl(): string {
  return URL;
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return { apikey: KEY, Authorization: `Bearer ${KEY}`, ...extra };
}

/** PostgREST select. `query` is a raw query string, e.g. "select=*&order=part_number". */
export async function sbSelect<T = Record<string, unknown>>(
  table: string,
  query = 'select=*',
): Promise<T[]> {
  if (!isSupabaseConfigured()) return [];
  const resp = await fetch(`${URL}/rest/v1/${table}?${query}`, { headers: authHeaders() });
  if (!resp.ok) throw new Error(`Supabase ${table}: HTTP ${resp.status}`);
  return (await resp.json()) as T[];
}

/** PostgREST upsert, merging on the conflict columns. */
export async function sbUpsert(
  table: string,
  rows: unknown[],
  onConflict: string,
): Promise<void> {
  if (!isSupabaseConfigured() || rows.length === 0) return;
  const resp = await fetch(`${URL}/rest/v1/${table}?on_conflict=${onConflict}`, {
    method: 'POST',
    headers: authHeaders({
      'Content-Type': 'application/json',
      Prefer: 'resolution=merge-duplicates,return=minimal',
    }),
    body: JSON.stringify(rows),
  });
  if (!resp.ok) throw new Error(`Supabase upsert ${table}: HTTP ${resp.status}`);
}

/** Upload a photo binary to Storage (upsert) and return its public URL. */
export async function sbUploadPhoto(objectPath: string, file: File): Promise<string> {
  if (!isSupabaseConfigured()) throw new Error('Supabase not configured');
  const path = objectPath.split('/').map(encodeURIComponent).join('/');
  const resp = await fetch(`${URL}/storage/v1/object/${SUPABASE_BUCKET}/${path}`, {
    method: 'POST',
    headers: authHeaders({
      'Content-Type': file.type || 'application/octet-stream',
      'x-upsert': 'true',
    }),
    body: file,
  });
  // 409 = object already present; treat as success (idempotent upload).
  if (!resp.ok && resp.status !== 409) throw new Error(`Storage upload: HTTP ${resp.status}`);
  return `${URL}/storage/v1/object/public/${SUPABASE_BUCKET}/${path}`;
}
