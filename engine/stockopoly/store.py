"""SQLite store for StockOpoly — WAL conventions ported from the Brain.

``stockopoly.sqlite`` lives at ``engine/data/stockopoly.sqlite`` unless the
``STOCKOPOLY_DB_PATH`` env var overrides it (tests must always override).
Schema creation is idempotent; columns are only ever added, never changed,
so ``init_schema()`` doubles as the migration runner.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parent.parent  # engine/


def data_dir() -> Path:
    """Root for all runtime artifacts (DB, staged photos, outbox JSONL).

    ``STOCKOPOLY_DATA_DIR`` override keeps tests hermetic; default engine/data
    is gitignored.
    """
    env = os.environ.get("STOCKOPOLY_DATA_DIR", "")
    if env:
        return Path(env).expanduser().resolve()
    return _ENGINE_DIR / "data"


def db_path() -> Path:
    env = os.environ.get("STOCKOPOLY_DB_PATH", "")
    if env:
        return Path(env).expanduser().resolve()
    return data_dir() / "stockopoly.sqlite"


def open_conn(timeout: float = 20.0, path: str | Path | None = None) -> sqlite3.Connection:
    p = Path(path).expanduser().resolve() if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    cn = sqlite3.connect(str(p), timeout=timeout)
    cn.row_factory = sqlite3.Row
    cn.execute("PRAGMA journal_mode=WAL")
    cn.execute("PRAGMA synchronous=NORMAL")
    cn.execute("PRAGMA wal_autocheckpoint=100")
    cn.execute("PRAGMA foreign_keys=ON")
    return cn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT, kind TEXT, payload_json TEXT);
CREATE TABLE IF NOT EXISTS scb_outbox(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT, kind TEXT, title TEXT, detail TEXT,
    signal_strength REAL, flushed INTEGER DEFAULT 0, flushed_at TEXT);

CREATE TABLE IF NOT EXISTS batches(
    batch_id TEXT PRIMARY KEY,
    kind TEXT,                    -- 'bundle' | 'loose'
    source_name TEXT, created_at TEXT,
    photo_count INTEGER DEFAULT 0, status TEXT DEFAULT 'new',
    manifest_json TEXT);
CREATE TABLE IF NOT EXISTS photos(
    photo_id TEXT PRIMARY KEY,
    batch_id TEXT REFERENCES batches(batch_id),
    file TEXT, abs_path TEXT, sha256 TEXT,
    captured_at TEXT, lat REAL, lng REAL, alt_m REAL,
    heading_deg REAL, pitch_deg REAL, roll_deg REAL,
    width INTEGER, height INTEGER, bytes INTEGER,
    focal_mm REAL, focal_35mm REAL, camera_model TEXT,
    blur_score REAL, dhash TEXT, ahash TEXT,
    exif_json TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS photo_groups(
    group_id TEXT PRIMARY KEY,
    batch_id TEXT, kind TEXT,
    -- 'same_object' | 'scale_reference' | 'relational_size' | 'location_label' | custom
    label TEXT, confidence REAL, source_tier INTEGER,  -- 1|2|3, 0 = manual
    meta_json TEXT, confirmed INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS photo_group_members(
    group_id TEXT REFERENCES photo_groups(group_id),
    photo_id TEXT REFERENCES photos(photo_id),
    role TEXT, confidence REAL,
    PRIMARY KEY(group_id, photo_id));

CREATE TABLE IF NOT EXISTS dim_entities(
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,                    -- 'object' | 'rack_member' | 'pattern_unit' | 'span' | 'distance'
    label TEXT, group_id TEXT, photo_id TEXT, notes TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS dim_variables(
    var_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER REFERENCES dim_entities(entity_id),
    axis TEXT,                    -- 'L' | 'W' | 'H' | 'SPAN'
    value REAL, value_ln REAL, sigma_ln REAL,
    ci_low REAL, ci_high REAL, confidence REAL, solved_at TEXT,
    UNIQUE(entity_id, axis));
CREATE TABLE IF NOT EXISTS photo_scales(
    scale_id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id TEXT, plane TEXT DEFAULT 'default',
    scale_ln REAL, sigma_ln REAL, solved_at TEXT,
    UNIQUE(photo_id, plane));
CREATE TABLE IF NOT EXISTS dim_measurements(
    meas_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    -- 'absolute' | 'pixel_extent' | 'ratio' | 'identity' | 'pattern_count'
    -- | 'sum_parts' | 'distance_estimate'
    photo_id TEXT, a_var INTEGER, b_var INTEGER, part_vars_json TEXT,
    value REAL, sigma REAL, unit TEXT DEFAULT 'in',
    source TEXT DEFAULT 'manual', -- 'manual'|'ocr'|'pattern'|'exif'|'llm'|'reference'
    meta_json TEXT, outlier INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS reference_objects(
    ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, kind TEXT,         -- 'ruler' | 'carton' | 'tool' | 'sku'
    dim_l REAL, dim_w REAL, dim_h REAL,
    unit TEXT DEFAULT 'in', sigma_pct REAL DEFAULT 1.0, notes TEXT);

CREATE TABLE IF NOT EXISTS locations(
    location_code TEXT PRIMARY KEY,
    aisle TEXT, bay TEXT, level INTEGER, bin TEXT, zone TEXT,
    x REAL, y REAL, z REAL, w REAL, d REAL, h REAL,
    capacity_volume REAL, fill_factor REAL DEFAULT 0.85,
    status TEXT DEFAULT 'active', -- 'active' | 'blocked' | 'reserve'
    source TEXT, confidence REAL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS layout(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,                    -- 'aisle' | 'dock' | 'staging'
    code TEXT, origin_x REAL, origin_y REAL, orientation_deg REAL DEFAULT 0,
    length REAL, width REAL, meta_json TEXT,
    UNIQUE(kind, code));

CREATE TABLE IF NOT EXISTS parts(
    part_number TEXT PRIMARY KEY,
    description TEXT, uom TEXT, unit_cost REAL,
    supplier_name TEXT, supplier_part_number TEXT, supplier_url TEXT,
    dim_l REAL, dim_w REAL, dim_h REAL, weight REAL,
    dim_unit TEXT DEFAULT 'in', weight_unit TEXT DEFAULT 'lb',
    dim_source TEXT, dim_confidence REAL,   -- 'crawl'|'manual'|'import'|'llm'
    norm_box_id TEXT, norm_qty_per_box REAL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS std_containers(
    box_id TEXT PRIMARY KEY,
    kind TEXT,                    -- 'carton' | 'tote' | 'pallet'
    name TEXT, l REAL, w REAL, h REAL,
    max_weight REAL, unit TEXT DEFAULT 'in');
CREATE TABLE IF NOT EXISTS inventory(
    part_number TEXT, location_code TEXT,
    qty_oh REAL, uom TEXT, as_of TEXT,
    PRIMARY KEY(part_number, location_code));
CREATE TABLE IF NOT EXISTS po_history(
    po_id TEXT, line_no INTEGER, part_number TEXT, supplier_name TEXT,
    qty REAL, unit_price REAL, order_date TEXT, receipt_date TEXT,
    PRIMARY KEY(po_id, line_no));
CREATE TABLE IF NOT EXISTS usage_history(
    part_number TEXT, period TEXT,        -- period = 'YYYY-MM'
    qty REAL, kind TEXT DEFAULT 'issue',
    PRIMARY KEY(part_number, period, kind));

CREATE TABLE IF NOT EXISTS crawl_cache(
    url TEXT PRIMARY KEY, domain TEXT, fetched_at TEXT,
    status INTEGER, content_hash TEXT, content_excerpt TEXT, robots_ok INTEGER);
CREATE TABLE IF NOT EXISTS crawl_results(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT, url TEXT,
    dim_l REAL, dim_w REAL, dim_h REAL, weight REAL,
    unit TEXT DEFAULT 'in', weight_unit TEXT DEFAULT 'lb',
    method TEXT,                  -- 'regex' | 'llm' | 'manual'
    confidence REAL, provenance_excerpt TEXT,
    accepted INTEGER DEFAULT 0, created_at TEXT);

CREATE TABLE IF NOT EXISTS velocity(
    part_number TEXT PRIMARY KEY,
    usage_12m_qty REAL, usage_12m_value REAL, monthly_hits INTEGER,
    abc_class TEXT, xyz_class TEXT, velocity_score REAL,
    months_of_supply REAL, computed_at TEXT);
CREATE TABLE IF NOT EXISTS occupancy(
    location_code TEXT PRIMARY KEY,
    used_volume REAL, capacity_volume REAL, occupancy_pct REAL,
    part_count INTEGER, status TEXT, computed_at TEXT);
CREATE TABLE IF NOT EXISTS slotting_plans(
    plan_id TEXT PRIMARY KEY, created_at TEXT, params_json TEXT,
    objective_before REAL, objective_after REAL,
    total_moves INTEGER, total_days INTEGER);
CREATE TABLE IF NOT EXISTS assignments_future(
    plan_id TEXT, part_number TEXT, location_code TEXT,
    qty_target REAL, role TEXT,   -- 'prime' | 'reserve'
    rank INTEGER, travel_cost REAL,
    PRIMARY KEY(plan_id, part_number, location_code));
CREATE TABLE IF NOT EXISTS move_tasks(
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT, day INTEGER, seq INTEGER,
    part_number TEXT, from_location TEXT, to_location TEXT, qty REAL,
    reason TEXT, est_minutes REAL, status TEXT DEFAULT 'todo', done_at TEXT);
CREATE TABLE IF NOT EXISTS ss_params(
    part_number TEXT PRIMARY KEY,
    demand_mean_m REAL, demand_std_m REAL, lead_time_days REAL,
    scenario TEXT DEFAULT 'baseline', overrides_json TEXT,
    ss_qty REAL, min_qty REAL, max_qty REAL, computed_at TEXT);

CREATE TABLE IF NOT EXISTS sync_state(
    key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""

# Additive column migrations for databases created before a column existed.
# ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so new columns
# are applied here. Each ALTER is idempotent: SQLite raises if the column is
# already present, which we swallow. Never drop or retype a column.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # Supabase cloud-mirror bookkeeping for photo binaries (Storage upload).
    ("photos", "remote_url", "TEXT"),
    ("photos", "synced_at", "TEXT"),
)


def _apply_column_migrations(cn: sqlite3.Connection) -> None:
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {r["name"] for r in cn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            cn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_schema(cn: sqlite3.Connection | None = None) -> None:
    own = cn is None
    if own:
        cn = open_conn()
    try:
        cn.executescript(_SCHEMA)
        _apply_column_migrations(cn)
        cn.commit()
    finally:
        if own:
            cn.close()
