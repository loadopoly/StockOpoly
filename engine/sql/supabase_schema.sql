-- StockOpoly → Supabase mirror schema.
--
-- Apply this once to the SAME Supabase project the Loadopoly Operate Console
-- uses (Dashboard → SQL Editor → paste → Run, or `supabase db push`). It is
-- idempotent: every object uses IF NOT EXISTS / ON CONFLICT so re-running is
-- safe. The StockOpoly engine (engine/stockopoly/supabase_sync.py) upserts
-- structured rows into these `stockopoly_*` tables via PostgREST and uploads
-- photo binaries into the `stockopoly-photos` Storage bucket.
--
-- Every row carries a `node` column (default 'stockopoly') so multiple engines
-- can share one project without clobbering each other.

-- ─────────────────────────────────────────────────────────── structured data
create table if not exists public.stockopoly_events (
  node text not null default 'stockopoly',
  id bigint not null,
  at text, kind text, payload_json text,
  primary key (node, id)
);

create table if not exists public.stockopoly_batches (
  node text not null default 'stockopoly',
  batch_id text primary key,
  kind text, source_name text, created_at text,
  photo_count bigint, status text, manifest_json text
);

create table if not exists public.stockopoly_groups (
  node text not null default 'stockopoly',
  group_id text primary key,
  batch_id text, kind text, label text, confidence double precision,
  source_tier bigint, meta_json text, confirmed bigint, created_at text
);

create table if not exists public.stockopoly_plans (
  node text not null default 'stockopoly',
  plan_id text primary key,
  created_at text, params_json text,
  objective_before double precision, objective_after double precision,
  total_moves bigint, total_days bigint
);

create table if not exists public.stockopoly_parts (
  node text not null default 'stockopoly',
  part_number text primary key,
  description text, uom text, unit_cost double precision,
  supplier_name text, supplier_part_number text, supplier_url text,
  dim_l double precision, dim_w double precision, dim_h double precision,
  weight double precision, dim_unit text, weight_unit text,
  dim_source text, dim_confidence double precision,
  norm_box_id text, norm_qty_per_box double precision, updated_at text
);

create table if not exists public.stockopoly_locations (
  node text not null default 'stockopoly',
  location_code text primary key,
  aisle text, bay text, level bigint, bin text, zone text,
  x double precision, y double precision, z double precision,
  w double precision, d double precision, h double precision,
  capacity_volume double precision, fill_factor double precision,
  status text, source text, confidence double precision, updated_at text
);

create table if not exists public.stockopoly_inventory (
  node text not null default 'stockopoly',
  part_number text not null, location_code text not null,
  qty_oh double precision, uom text, as_of text,
  primary key (part_number, location_code)
);

create table if not exists public.stockopoly_velocity (
  node text not null default 'stockopoly',
  part_number text primary key,
  usage_12m_qty double precision, usage_12m_value double precision,
  monthly_hits bigint, abc_class text, xyz_class text,
  velocity_score double precision, months_of_supply double precision, computed_at text
);

create table if not exists public.stockopoly_occupancy (
  node text not null default 'stockopoly',
  location_code text primary key,
  used_volume double precision, capacity_volume double precision,
  occupancy_pct double precision, part_count bigint, status text, computed_at text
);

create table if not exists public.stockopoly_assignments (
  node text not null default 'stockopoly',
  plan_id text not null, part_number text not null, location_code text not null,
  qty_target double precision, role text, rank bigint, travel_cost double precision,
  primary key (plan_id, part_number, location_code)
);

create table if not exists public.stockopoly_move_tasks (
  node text not null default 'stockopoly',
  task_id bigint not null,
  plan_id text, day bigint, seq bigint, part_number text,
  from_location text, to_location text, qty double precision,
  reason text, est_minutes double precision, status text, done_at text,
  primary key (node, task_id)
);

create table if not exists public.stockopoly_safety_stock (
  node text not null default 'stockopoly',
  part_number text primary key,
  demand_mean_m double precision, demand_std_m double precision,
  lead_time_days double precision, scenario text, overrides_json text,
  ss_qty double precision, min_qty double precision, max_qty double precision, computed_at text
);

create table if not exists public.stockopoly_photos (
  node text not null default 'stockopoly',
  photo_id text primary key,
  batch_id text, file text, sha256 text, captured_at text,
  lat double precision, lng double precision,
  width bigint, height bigint, bytes bigint, blur_score double precision,
  camera_model text, remote_url text
);

-- ───────────────────────────────────────────────────────────── RLS policies
-- Permissive by design: this is shared operational data the hosted dashboard
-- reads (and the engine upserts) with the project anon/service key. Tighten to
-- your org's needs if these tables hold sensitive values.
do $$
declare t text;
begin
  foreach t in array array[
    'stockopoly_events','stockopoly_batches','stockopoly_groups','stockopoly_plans',
    'stockopoly_parts','stockopoly_locations','stockopoly_inventory','stockopoly_velocity',
    'stockopoly_occupancy','stockopoly_assignments','stockopoly_move_tasks',
    'stockopoly_safety_stock','stockopoly_photos'
  ]
  loop
    execute format('alter table public.%I enable row level security', t);
    if not exists (
      select 1 from pg_policies
      where schemaname='public' and tablename=t and policyname='stockopoly_all'
    ) then
      execute format(
        'create policy stockopoly_all on public.%I for all to anon, authenticated using (true) with check (true)', t);
    end if;
  end loop;
end $$;

-- ──────────────────────────────────────────────────────────── photo Storage
insert into storage.buckets (id, name, public)
values ('stockopoly-photos', 'stockopoly-photos', true)
on conflict (id) do nothing;

do $$
begin
  if not exists (select 1 from pg_policies
    where schemaname='storage' and tablename='objects'
      and policyname='stockopoly photos read') then
    create policy "stockopoly photos read" on storage.objects for select
      to public using (bucket_id = 'stockopoly-photos');
  end if;

  if not exists (select 1 from pg_policies
    where schemaname='storage' and tablename='objects'
      and policyname='stockopoly photos write') then
    create policy "stockopoly photos write" on storage.objects for insert
      to anon, authenticated with check (bucket_id = 'stockopoly-photos');
  end if;

  if not exists (select 1 from pg_policies
    where schemaname='storage' and tablename='objects'
      and policyname='stockopoly photos update') then
    create policy "stockopoly photos update" on storage.objects for update
      to anon, authenticated using (bucket_id = 'stockopoly-photos');
  end if;
end $$;
