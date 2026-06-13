// Mirrors the engine's JSON shapes (stockopoly.api/1). Kept intentionally
// loose where the engine returns open dicts (events, summaries).

export interface StatusResponse {
  api: string;
  version: string;
  db: string;
  data_dir: string;
  counts: Record<string, number>;
  scb: ScbStatus;
  erp: ErpStatus;
  app_built: boolean;
}

export interface ScbStatus {
  scb_repo: string | null;
  scb_db: string | null;
  scb_db_exists: boolean;
  outbox_pending: number;
  outbox_jsonl: string;
}

export interface ErpStatus {
  enabled: boolean;
  scb_repo: string | null;
  importable: boolean;
  reason: string | null;
}

export interface Batch {
  batch_id: string;
  kind: 'bundle' | 'loose';
  source_name: string | null;
  created_at: string;
  photo_count: number;
  status: string;
}

export interface Photo {
  photo_id: string;
  file: string;
  abs_path: string;
  sha256: string | null;
  captured_at: string | null;
  lat: number | null;
  lng: number | null;
  alt_m: number | null;
  heading_deg: number | null;
  pitch_deg: number | null;
  roll_deg: number | null;
  width: number | null;
  height: number | null;
  bytes: number | null;
  focal_mm: number | null;
  focal_35mm: number | null;
  camera_model: string | null;
  blur_score: number | null;
  dhash: string | null;
  ahash: string | null;
}

export interface Group {
  group_id: string;
  kind: string;
  label: string;
  confidence: number;
  source_tier: number;
  confirmed: boolean;
  meta: Record<string, unknown>;
  photo_ids: string[];
}

export interface CascadeResult {
  batch_id: string;
  tiers_run: number[];
  created: number;
  group_count: number;
  groups: Group[];
}

export interface DimVariable {
  var_id: number;
  axis: string;
  value: number | null;
  sigma_ln: number | null;
  ci_low: number | null;
  ci_high: number | null;
  confidence: number | null;
  solved_at: string | null;
}

export interface DimEntity {
  entity_id: number;
  kind: string;
  label: string;
  group_id: string | null;
  photo_id: string | null;
  notes: string | null;
  variables: DimVariable[];
}

export interface SolveReport {
  variables: number;
  photo_scales: number;
  measurements: number;
  outliers: number[];
  iterations: number;
  rms: number;
  components: { grounded: boolean; anchor_count: number; members: string[] }[];
  ungrounded_components: number;
  solved_at: string;
}

export interface ReferenceObject {
  ref_id: number;
  name: string;
  kind: string;
  dim_l: number | null;
  dim_w: number | null;
  dim_h: number | null;
  unit: string;
  sigma_pct: number;
  notes: string | null;
}

export interface LocationRow {
  location_code: string;
  aisle: string;
  bay: string;
  level: number;
  bin: string | null;
  zone: string | null;
  x: number | null;
  y: number | null;
  z: number | null;
  w: number | null;
  d: number | null;
  h: number | null;
  capacity_volume: number | null;
  fill_factor: number | null;
  status: string;
  occupancy_pct?: number | null;
  occ_status?: string | null;
  part_count?: number | null;
}

export interface LayoutRow {
  id: number;
  kind: string;
  code: string;
  origin_x: number;
  origin_y: number;
  orientation_deg: number;
  length: number | null;
  width: number | null;
}

export interface MapData {
  locations: LocationRow[];
  layout: LayoutRow[];
  future: Record<string, { parts: number; qty: number }>;
}

export interface Part {
  part_number: string;
  description: string | null;
  uom: string | null;
  unit_cost: number | null;
  supplier_name: string | null;
  supplier_url: string | null;
  dim_l: number | null;
  dim_w: number | null;
  dim_h: number | null;
  weight: number | null;
  dim_source: string | null;
  norm_box_id: string | null;
  norm_qty_per_box: number | null;
  abc_class: string | null;
  xyz_class: string | null;
  velocity_score: number | null;
  months_of_supply: number | null;
  ss_qty: number | null;
  min_qty: number | null;
  max_qty: number | null;
}

export interface CrawlResult {
  id: number;
  part_number: string;
  url: string;
  dim_l: number | null;
  dim_w: number | null;
  dim_h: number | null;
  weight: number | null;
  method: string;
  confidence: number;
  provenance_excerpt: string | null;
  accepted: number;
}

export interface VelocityRow {
  part_number: string;
  usage_12m_qty: number;
  usage_12m_value: number;
  monthly_hits: number;
  abc_class: string;
  xyz_class: string;
  velocity_score: number;
  months_of_supply: number | null;
}

export interface OccupancyRow extends LocationRow {
  used_volume: number;
  occupancy_pct: number;
  status: string;
  inventory: { part_number: string; qty_oh: number; uom: string | null }[];
}

export interface Plan {
  plan_id: string;
  created_at: string;
  objective_before: number;
  objective_after: number;
  total_moves: number;
  total_days: number;
}

export interface OptimizeSummary {
  plan_id: string;
  parts_assigned: number;
  objective_before: number;
  objective_after: number;
  improvement_pct: number;
  swap_count: number;
}

export interface Assignment {
  plan_id: string;
  part_number: string;
  location_code: string;
  qty_target: number;
  role: string;
  rank: number;
  travel_cost: number | null;
}

export interface MoveTask {
  task_id: number;
  plan_id: string;
  day: number;
  seq: number;
  part_number: string;
  from_location: string;
  to_location: string;
  qty: number;
  reason: string;
  est_minutes: number;
  status: string;
}

export interface SafetyStockRow {
  part_number: string;
  demand_mean_m: number;
  demand_std_m: number;
  lead_time_days: number;
  scenario: string;
  ss_qty: number;
  min_qty: number;
  max_qty: number;
}

export interface ScenarioCompare {
  part_number: string;
  demand_mean_m: number;
  demand_std_m: number;
  lead_time_days: number;
  scenarios: Record<string, { z: number; ss_qty: number; min_qty: number; max_qty: number }>;
}

export interface EngineEvent {
  at: string;
  kind: string;
  payload: Record<string, unknown>;
}

export type Settings = Record<string, unknown>;
