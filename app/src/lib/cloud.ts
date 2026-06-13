// Cloud (Supabase-only) data source. When no engine is reachable — e.g. the app
// is hosted on GitHub Pages — these read the stockopoly_* tables the engine
// mirrors, and capture upload writes straight to Storage + the photos table.
import { isSupabaseConfigured, sbSelect, sbUploadPhoto, sbUpsert } from './supabase';
import type { CloudPhoto, LocationRow, Part } from './types';

export { isSupabaseConfigured };

const n = (v: unknown): number | null =>
  typeof v === 'number' ? v : v == null ? null : Number(v);
const s = (v: unknown): string | null => (v == null ? null : String(v));

export async function cloudCounts(): Promise<Record<string, number>> {
  const [parts, locations, plans, photos, tasks, inventory] = await Promise.all([
    sbSelect('stockopoly_parts', 'select=part_number'),
    sbSelect('stockopoly_locations', 'select=location_code'),
    sbSelect('stockopoly_plans', 'select=plan_id'),
    sbSelect('stockopoly_photos', 'select=photo_id'),
    sbSelect('stockopoly_move_tasks', 'select=task_id'),
    sbSelect('stockopoly_inventory', 'select=part_number'),
  ]);
  return {
    parts: parts.length,
    locations: locations.length,
    slotting_plans: plans.length,
    photos: photos.length,
    move_tasks: tasks.length,
    inventory: inventory.length,
  };
}

export async function cloudParts(): Promise<Part[]> {
  const [rows, vel] = await Promise.all([
    sbSelect('stockopoly_parts', 'select=*&order=part_number'),
    sbSelect('stockopoly_velocity', 'select=*'),
  ]);
  const velByPn = new Map<string, Record<string, unknown>>(
    vel.map((v) => [String(v.part_number), v]),
  );
  return rows.map((r): Part => {
    const v = velByPn.get(String(r.part_number)) ?? {};
    return {
      part_number: String(r.part_number),
      description: s(r.description),
      uom: s(r.uom),
      unit_cost: n(r.unit_cost),
      supplier_name: s(r.supplier_name),
      supplier_url: s(r.supplier_url),
      dim_l: n(r.dim_l),
      dim_w: n(r.dim_w),
      dim_h: n(r.dim_h),
      weight: n(r.weight),
      dim_source: s(r.dim_source),
      norm_box_id: s(r.norm_box_id),
      norm_qty_per_box: n(r.norm_qty_per_box),
      abc_class: s(v.abc_class),
      xyz_class: s(v.xyz_class),
      velocity_score: n(v.velocity_score),
      months_of_supply: n(v.months_of_supply),
      ss_qty: null,
      min_qty: null,
      max_qty: null,
    };
  });
}

export async function cloudLocations(): Promise<LocationRow[]> {
  const [rows, occ] = await Promise.all([
    sbSelect('stockopoly_locations', 'select=*&order=location_code'),
    sbSelect('stockopoly_occupancy', 'select=*'),
  ]);
  const occByCode = new Map<string, Record<string, unknown>>(
    occ.map((o) => [String(o.location_code), o]),
  );
  return rows.map((r): LocationRow => {
    const o = occByCode.get(String(r.location_code)) ?? {};
    return {
      location_code: String(r.location_code),
      aisle: String(r.aisle ?? ''),
      bay: String(r.bay ?? ''),
      level: Number(r.level ?? 0),
      bin: s(r.bin),
      zone: s(r.zone),
      x: n(r.x),
      y: n(r.y),
      z: n(r.z),
      w: n(r.w),
      d: n(r.d),
      h: n(r.h),
      capacity_volume: n(r.capacity_volume),
      fill_factor: n(r.fill_factor),
      status: String(r.status ?? 'active'),
      occupancy_pct: n(o.occupancy_pct),
      occ_status: s(o.status),
      part_count: n(o.part_count),
    };
  });
}

export async function cloudPhotos(limit = 60): Promise<CloudPhoto[]> {
  const rows = await sbSelect(
    'stockopoly_photos',
    `select=*&order=captured_at.desc.nullslast&limit=${limit}`,
  );
  return rows.map((r): CloudPhoto => ({
    photo_id: String(r.photo_id),
    batch_id: s(r.batch_id),
    file: s(r.file),
    remote_url: s(r.remote_url),
    captured_at: s(r.captured_at),
    blur_score: n(r.blur_score),
  }));
}

// Capture upload straight to the cloud (no engine): push each photo binary to
// Storage and record a stockopoly_photos row, under one batch.
export async function uploadCaptureToCloud(
  files: File[],
  batchName?: string,
): Promise<{ batch_id: string; uploaded: number; failed: number }> {
  const batchId = `cloud-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
  const now = new Date().toISOString();
  let uploaded = 0;
  let failed = 0;
  const metas: Record<string, unknown>[] = [];

  for (const file of files) {
    const object = `${batchId}/${file.name}`;
    try {
      const url = await sbUploadPhoto(object, file);
      metas.push({
        node: 'app',
        photo_id: `${batchId}/${file.name}`,
        batch_id: batchId,
        file: file.name,
        captured_at: file.lastModified ? new Date(file.lastModified).toISOString() : now,
        bytes: file.size,
        remote_url: url,
      });
      uploaded += 1;
    } catch {
      failed += 1;
    }
  }

  if (metas.length) {
    await sbUpsert('stockopoly_photos', metas, 'photo_id');
    await sbUpsert(
      'stockopoly_batches',
      [
        {
          node: 'app',
          batch_id: batchId,
          kind: 'loose',
          source_name: batchName ?? `cloud upload ${files.length} files`,
          created_at: now,
          photo_count: uploaded,
          status: 'new',
        },
      ],
      'batch_id',
    );
  }
  return { batch_id: batchId, uploaded, failed };
}
