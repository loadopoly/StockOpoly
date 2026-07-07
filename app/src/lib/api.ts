// Typed client for the StockOpoly engine (stockopoly.api/1). All calls are
// same-origin: in dev Vite proxies /api and /intake to the engine on :8181;
// in production the engine serves this built UI itself.
import type {
  Assignment,
  Batch,
  CascadeResult,
  CrawlResult,
  DimEntity,
  EngineEvent,
  ErpStatus,
  Group,
  LocationRow,
  MapData,
  MoveTask,
  OccupancyRow,
  OptimizeSummary,
  Part,
  Photo,
  Plan,
  ReferenceObject,
  SafetyStockRow,
  ScbStatus,
  ScenarioCompare,
  Settings,
  SolveReport,
  StatusResponse,
  SyncResult,
  SyncStatus,
  VelocityRow,
} from './types';

// Where the engine API lives. Same-origin by default (dev proxy / engine-served
// build); VITE_ENGINE_URL lets a statically hosted app target a remote engine.
const ENGINE_BASE = (import.meta.env.VITE_ENGINE_URL ?? '').replace(/\/+$/, '');

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
}

const REQUEST_TIMEOUT_MS = 30_000;
const UPLOAD_TIMEOUT_MS = 120_000;

// fetch with a hard deadline — a wedged engine must not leave the UI pending
// forever. AbortError surfaces as a normal rejection the callers already handle.
async function fetchWithTimeout(
  input: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  const resp = await fetchWithTimeout(`${ENGINE_BASE}${path}`, init, REQUEST_TIMEOUT_MS);
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!resp.ok) {
    const msg =
      data && typeof data === 'object' && 'error' in data
        ? String((data as { error: unknown }).error)
        : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, msg);
  }
  return data as T;
}

const get = <T>(p: string) => request<T>('GET', p);
const post = <T>(p: string, body?: unknown) => request<T>('POST', p, body);
const put = <T>(p: string, body?: unknown) => request<T>('PUT', p, body);
const del = <T>(p: string) => request<T>('DELETE', p);

async function upload<T>(path: string, form: FormData): Promise<T> {
  const resp = await fetchWithTimeout(
    `${ENGINE_BASE}${path}`, { method: 'POST', body: form }, UPLOAD_TIMEOUT_MS);
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const msg = data?.error ? String(data.error) : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, msg);
  }
  return data as T;
}

export const api = {
  status: () => get<StatusResponse>('/api/status'),
  scbStatus: () => get<ScbStatus>('/api/scb/status'),
  scbFlush: () => post<{ flushed: number }>('/api/scb/flush'),
  events: (limit = 50) => get<{ events: EngineEvent[] }>(`/api/events?limit=${limit}`),

  // intake
  batches: () => get<{ batches: Batch[] }>('/api/batches'),
  batchPhotos: (id: string) =>
    get<{ photos: Photo[] }>(`/api/batches/${encodeURIComponent(id)}/photos`),
  photoUrl: (batchId: string, file: string) =>
    `${ENGINE_BASE}/api/photos/${encodeURIComponent(batchId)}/${encodeURIComponent(file)}`,
  uploadBundle: (file: File) => {
    const form = new FormData();
    form.append('bundle', file, file.name);
    return upload<{ ok: boolean; batch_id?: string; duplicate?: boolean }>('/intake', form);
  },
  uploadLoose: (files: File[], name?: string) => {
    const form = new FormData();
    files.forEach((f) => form.append('photos', f, f.name));
    const q = name ? `?name=${encodeURIComponent(name)}` : '';
    return upload<{ batch_id: string; photo_count: number; geotagged: number }>(
      `/api/intake/loose${q}`,
      form,
    );
  },

  // grouping
  groups: (batchId: string) =>
    get<{ groups: Group[] }>(`/api/groups?batch_id=${encodeURIComponent(batchId)}`),
  cascade: (batchId: string) => post<CascadeResult>('/api/groups/cascade', { batch_id: batchId }),
  createGroup: (batch_id: string, kind: string, label: string, photo_ids: string[]) =>
    post<{ group_id: string }>('/api/groups', { batch_id, kind, label, photo_ids }),
  confirmGroup: (gid: string, confirmed: boolean) =>
    post<{ ok: boolean }>(`/api/groups/${gid}/confirm`, { confirmed }),
  setGroupMembers: (gid: string, photo_ids: string[]) =>
    put<{ ok: boolean }>(`/api/groups/${gid}/members`, { photo_ids }),
  deleteGroup: (gid: string) => del<{ ok: boolean }>(`/api/groups/${gid}`),

  // dims
  entities: () => get<{ entities: DimEntity[] }>('/api/dims/entities'),
  createEntity: (kind: string, label: string, group_id?: string) =>
    post<{ entity_id: number }>('/api/dims/entities', { kind, label, group_id }),
  ensureVariable: (entity_id: number, axis: string) =>
    post<{ var_id: number }>('/api/dims/variables', { entity_id, axis }),
  addMeasurement: (m: Record<string, unknown>) =>
    post<{ meas_id: number }>('/api/dims/measurements', m),
  deleteMeasurement: (mid: number) => del<{ ok: boolean }>(`/api/dims/measurements/${mid}`),
  solve: () => post<SolveReport>('/api/dims/solve'),
  references: () => get<{ references: ReferenceObject[] }>('/api/dims/references'),
  addReference: (r: Record<string, unknown>) =>
    post<{ ref_id: number }>('/api/dims/references', r),
  applyReference: (rid: number, photo_id: string, pixel_extents: Record<string, number>) =>
    post<Record<string, unknown>>(`/api/dims/references/${rid}/apply`, { photo_id, pixel_extents }),

  // locations
  locations: (zone?: string) =>
    get<{ locations: LocationRow[] }>(`/api/locations${zone ? `?zone=${zone}` : ''}`),
  registerLocations: (codes: string[], zone?: string) =>
    post<{ added: number; updated: number; failed: string[] }>('/api/locations/register', {
      codes,
      zone,
    }),
  computeCoordinates: () =>
    post<{ located: number; aisles: number; bay_width_in: number }>('/api/locations/coordinates'),
  travelCosts: () => get<{ costs: Record<string, number> }>('/api/locations/travel'),
  setDock: (x: number, y: number) => post<{ ok: boolean }>('/api/locations/dock', { x, y }),
  map: (planId?: string) => get<MapData>(`/api/map${planId ? `?plan_id=${planId}` : ''}`),

  // imports + parts
  importRows: (target: string, rows: Record<string, unknown>[]) =>
    post<{ imported: number; skipped: number }>(`/api/import/${target}`, { rows }),
  importFile: (target: string, file: File) => {
    const form = new FormData();
    form.append('file', file, file.name);
    return upload<{ imported: number; skipped: number }>(`/api/import/${target}`, form);
  },
  parts: (q?: string) => get<{ parts: Part[] }>(`/api/parts${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  normalize: () => post<{ normalized: number; missing_dims: number }>('/api/partdims/normalize'),
  erpStatus: () => get<ErpStatus>('/api/erp/status'),

  // crawler
  crawlResults: (all = false) =>
    get<{ results: CrawlResult[] }>(`/api/crawl/results${all ? '?all=1' : ''}`),
  crawlPart: (part_number: string) =>
    post<{ status: string; results: number }>('/api/crawl/part', { part_number }),
  acceptCrawl: (rid: number) => post<Record<string, unknown>>(`/api/crawl/results/${rid}/accept`),
  rejectCrawl: (rid: number) => post<{ ok: boolean }>(`/api/crawl/results/${rid}/reject`),

  // slotting
  computeVelocity: () => post<{ parts: number; classes: Record<string, number> }>('/api/slotting/velocity'),
  velocity: () => get<{ velocity: VelocityRow[] }>('/api/slotting/velocity'),
  computeOccupancy: () =>
    post<{ locations: number; over: number; tight: number; ok: number; empty: number }>(
      '/api/slotting/occupancy',
    ),
  currentState: (status?: string) =>
    get<{ locations: OccupancyRow[] }>(`/api/slotting/current${status ? `?status=${status}` : ''}`),
  optimize: (params?: Record<string, unknown>) =>
    post<OptimizeSummary>('/api/slotting/optimize', params ?? {}),
  plans: () => get<{ plans: Plan[] }>('/api/slotting/plans'),
  planAssignments: (pid: string) =>
    get<{ assignments: Assignment[] }>(`/api/slotting/plans/${pid}/assignments`),
  buildTasks: (pid: string) =>
    post<{ moves: number; days: number }>(`/api/slotting/plans/${pid}/tasks`),
  tasks: (pid: string, day?: number) =>
    get<{ tasks: MoveTask[] }>(`/api/slotting/plans/${pid}/tasks${day ? `?day=${day}` : ''}`),
  completeTask: (tid: number) => post<MoveTask>(`/api/slotting/tasks/${tid}/complete`),
  computeSafetyStock: (scenario?: string) =>
    post<{ scenario: string; parts: number }>('/api/slotting/safety-stock', { scenario }),
  safetyStock: () => get<{ ss: SafetyStockRow[] }>('/api/slotting/safety-stock'),
  scenarioCompare: (part: string) =>
    get<ScenarioCompare>(`/api/slotting/safety-stock/${encodeURIComponent(part)}/compare`),

  // settings
  settings: () => get<Settings>('/api/settings'),
  updateSettings: (values: Settings) => put<Settings>('/api/settings', values),

  // supabase cloud mirror
  syncStatus: () => get<SyncStatus>('/api/sync/status'),
  syncNow: (tables?: string[]) => post<SyncResult>('/api/sync/now', tables ? { tables } : {}),
};

// Probe whether a local/remote engine is reachable. Used to fall back to the
// Supabase-only "cloud" experience when the app is hosted on GitHub Pages with
// no engine behind it.
export async function engineReachable(timeoutMs = 2500): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const resp = await fetch(`${ENGINE_BASE}/api/status`, { signal: ctrl.signal });
    return resp.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
