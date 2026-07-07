# Changelog

All notable changes to **StockOpoly** are documented here. Versions follow
[Semantic Versioning](https://semver.org). The single source of truth for the
version number is the root `VERSION` file.

## [0.2.0] Security & Robustness Hardening (2026-07-07)

Hardening pass over the cloud mirror, HTTP API, and app cloud client that
shipped in 0.1.0. No new features — every change closes a defect or a
security gap found in review.

### Security & hardening

- **Path traversal → arbitrary file exfiltration (fixed, defence in depth):**
  a bundle manifest `photos[].file` that escaped the staged bundle dir was
  stored verbatim in `photos.abs_path` and could later be read and uploaded to
  the public Storage bucket. Now blocked at **ingest** (`intake/__init__.py`
  skips escaping entries) **and** at **upload** (`_sync_photos` re-checks that
  each `abs_path` resolves inside the engine data dir).
- **RLS tightened** (`engine/sql/supabase_schema.sql`): the public anon key
  (baked into the hosted build) is scoped to `select` / `insert` / `update`
  only — the over-broad `for all` policy (which granted anon **DELETE**) is
  dropped. Storage policies already omit delete.
- **CORS** (`server.py`): the JSON API now reflects only allowlisted origins
  (localhost dev ports by default, extensible via `STOCKOPOLY_CORS_ORIGINS`)
  instead of `*`; the Brain-receiver-compatible `/intake` aliases stay
  wildcard for Operate-Console uplink.
- **Sync coalescing correctness** (`supabase_sync.py`): a trigger that arrives
  mid-run now sets a pending flag so the running thread does one more pass —
  the last mutation of a burst always reaches the cloud instead of being
  dropped.
- **Server robustness** (`server.py`): a non-numeric `Content-Length` returns
  a 400 instead of resetting the connection; a 60 s socket timeout guards
  against slow-loris; `POST /api/sync/now` validates `tables` is a list of
  strings; path-containment checks use a real ancestor test instead of a
  string prefix.
- **Incremental sync** pages to exhaustion (drains a >500-row backlog in one
  pass) with a `rowid` ordering tiebreaker.
- **App fetch timeouts** (`api.ts`, `supabase.ts`): 30 s for JSON
  requests/selects/upserts, 120 s for uploads, so a black-holed connection
  can't leave a cloud loader spinning forever.
- **Storage URLs** are percent-encoded per path segment on the Python side,
  matching the TS client — filenames with spaces/`#`/`?` now round-trip.

### Verification

- `python -m pytest -q` (engine) — full suite green, including new tests for
  the traversal skip, the out-of-data-dir upload guard, and the sync
  pending-rerun path
- `npm run typecheck && npm run lint && npm run build` (app) — green

## [0.1.0] Initial Engine + App + Cloud Mirror (2026-06-12)

### Added

- **Engine** (`engine/`, Python ≥3.10, stdlib-first): `loadopoly.capture/1`
  bundle + loose-JPEG intake with SHA-256 fixity; photo-grouping cascade
  (SCB vision → offline heuristics → optional LLM); relational dimension
  solver (log-space WLS + sum-of-parts, Huber outliers, ungrounded-component
  detection); label grammar → 3D location graph → Dijkstra travel costs;
  CSV/XLSX imports + optional ERP bridge; supplier-dimension crawler
  (robots-aware, rate-limited, human-Accept gate); ABC×XYZ velocity,
  occupancy, greedy+swap slotting optimizer, day-by-day migration tasks,
  3-scenario safety stock; stdlib `ThreadingHTTPServer` JSON API
  (`stockopoly.api/1`) wire-compatible with the Brain's photogrammetry
  receiver; 96-bin synthetic demo.
- **Automatic Supabase mirror** (`engine/stockopoly/supabase_sync.py`): every
  successful mutation fires a non-blocking background sync that pushes
  structured rows to PostgREST `stockopoly_*` tables **and uploads photo
  binaries** to the `stockopoly-photos` Storage bucket — gated by
  `share_supabase` / `auto_sync` / env, idempotent photo uploads
  (`synced_at`), DDL at `engine/sql/supabase_schema.sql`. CLI `sync`
  subcommand + `GET /api/sync/status` + `POST /api/sync/now`.
- **Always-on SCB learning** (`scb_link.py`): appends to the Brain's
  `learning_log`, queues locally when the Brain is absent.
- **App** (`app/`, React 19 + Vite + TS + Tailwind + react-three-fiber):
  nine tabs (Intake · Groups · Measure · Locations · Inventory · Slotting ·
  Optimize · Map3D · Settings) over the engine API, plus a **cloud mode**
  (`lib/supabase.ts`, `lib/cloud.ts`, `components/cloud/CloudView.tsx`) that
  reads the mirrored `stockopoly_*` tables and uploads captures straight to
  Storage when no engine is reachable.
- **GitHub Pages deployment** (`.github/workflows/deploy-pages.yml`): mirrors
  the Loadopoly-OCR Actions flow with the same `VITE_SUPABASE_*` variables.
