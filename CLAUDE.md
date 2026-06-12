# StockOpoly — Claude Code Project Guide

Standalone, lightweight **warehouse mapping + slotting** app. Third sibling of a
three-repo system:

| Repo | Role |
|---|---|
| `loadopoly/StockOpoly` (this) | Photo-derived dimension solving, 3D location space, current/future-state slotting |
| `loadopoly/Loadopoly-OCR` (`../Loadopoly-OCR`) | React field-capture PWA — produces `loadopoly.capture/1` bundles |
| `loadopoly/Supply-Chain-Brain` (`../Supply-Chain-Brain`) | Python analytics Brain — learning_log / safety-stock / ERP connectors |

Two halves in one repo: **`engine/`** (Python ≥3.10, stdlib-first) and **`app/`**
(React 19 + Vite + TS + Tailwind + react-three-fiber).

## Run

```bash
cd engine
python -m stockopoly init        # create stockopoly.sqlite schema
python -m stockopoly demo        # synthetic 96-bin demo warehouse (no network)
python -m stockopoly serve       # API + built UI on http://localhost:8181
python -m pytest -q              # engine test suite

cd ../app
npm install
npm run dev                      # UI on http://localhost:3001 (proxies /api → 8181)
npm run typecheck && npm run lint && npm run build
```

Ports: engine **8181**, app dev **3001** — deliberately clear of the existing
orchestra (OCR app 3000, SCB receiver 8787, Streamlit 8501).

## Map

- `engine/stockopoly/store.py` — `stockopoly.sqlite` (WAL; `STOCKOPOLY_DB_PATH`
  override). **Tests must point STOCKOPOLY_DB_PATH at a temp file** (conftest
  enforces this) — never the real DB.
- `engine/stockopoly/scb_link.py` — **always-on learning**: appends to the
  Brain's `learning_log` (sibling discovery; `SCB_REPO_DIR`/`SCB_DB_PATH`
  honoured). Unreachable → `scb_outbox` + `data/scb_learning_outbox.jsonl`
  (cloud_learning_queue-shaped). Independent of the Supabase sharing toggle.
- `engine/stockopoly/intake/` — `loadopoly.capture/1` bundle intake (vendored
  stdlib manifest reader — **must stay byte-compatible with
  `../Loadopoly-OCR/src/capture/types.ts`**) + loose-JPEG batches (EXIF, dhash).
- `engine/stockopoly/grouping/` — photo-grouping cascade: tier 1 SCB vision →
  tier 2 offline heuristics → tier 3 OpenRouter/Grok (env-gated).
- `engine/stockopoly/dims/` — **relational dimension solver** (log-space WLS +
  linear sum-parts stage, Huber outliers, ungrounded-component detection).
  The algorithmic heart; `tests/test_solver.py` is its gate.
- `engine/stockopoly/locations/` — label grammar → location graph → coordinates
  → travel costs (Dijkstra to dock).
- `engine/stockopoly/imports/` — CSV/XLSX (stdlib) loaders for inventory / PO /
  usage / parts; optional ERP bridge via SCB `data_access` (`STOCKOPOLY_ERP=1`).
- `engine/stockopoly/partdims/` — std carton/pallet normalization + directed
  supplier-dimension crawler (robots-aware, rate-limited, cached, human Accept
  required before dims land on parts).
- `engine/stockopoly/slotting/` — occupancy/current-state lists, ABC×XYZ
  velocity, optimizer (greedy + swaps), day-by-day migration tasks, 3-scenario
  safety stock (conservative/baseline/aggressive).
- `engine/stockopoly/server.py` — stdlib ThreadingHTTPServer JSON API
  (`stockopoly.api/1`) + static `app/dist`; **bundle intake is wire-compatible
  with the Brain's receiver** (multipart `bundle`+`session`, HEAD probe), so the
  Operate Console can uplink here by setting its intake URL to
  `http://<host>:8181/intake` — zero OCR-app changes.
- `app/src/` — tab UI: Intake · Groups · Measure · Locations · Inventory ·
  Slotting · Optimize · Map3D (instanced bins, current/future/overlay) ·
  Settings.

## Rules

- **Engine core is stdlib-only.** Pillow / requests / numpy are optional
  accelerators (`requirements-optional.txt`); every import of them must degrade
  gracefully. No pandas, no web frameworks.
- Raw photos and SQLite files live under `engine/data/` and are **gitignored —
  never commit captured data**.
- Supabase sync (`supabase_sync.py`) is sharing-toggleable and a clean no-op
  without env config. SCB learning writes are NOT gated by that toggle.
- Frontend follows the Loadopoly-OCR design system: Tailwind dark-slate,
  `primary` = blue, large touch targets, ARIA labels on every interactive
  element.
- Contract changes to the vendored `loadopoly.capture/1` reader must follow the
  producer repos (see `docs/CONTRACTS.md`); `tests/test_manifest.py` pins the
  schema literal.
- Verify before finishing: `python -m pytest -q` (engine) and
  `npm run typecheck && npm run lint && npm run build` (app).
