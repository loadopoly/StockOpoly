# StockOpoly

Warehouse mapping + slotting from field photos. Third sibling of the
Loadopoly system (alongside the **Loadopoly-OCR** capture PWA and the
**Supply-Chain-Brain** analytics engine): ingest photo bundles, group and
measure objects into a relational dimension solve, build a 3D location space,
and turn current-state occupancy into an optimized future-state slotting plan
with a day-by-day migration list.

Two halves in one repo:

- **`engine/`** — Python ≥ 3.10, stdlib-first. The analytical core + a
  stdlib HTTP API (`stockopoly.api/1`).
- **`app/`** — React 19 + Vite + TypeScript + Tailwind + react-three-fiber.

See **`CLAUDE.md`** for the full project guide and **`docs/CONTRACTS.md`** for
the cross-repo contracts this project consumes.

## Quick start

```bash
# Engine
cd engine
python -m stockopoly init        # create stockopoly.sqlite
python -m stockopoly demo        # synthetic 96-bin warehouse, end-to-end
python -m stockopoly serve       # API + built UI + /intake on :8181
python -m pytest -q              # 88 tests

# App (dev)
cd ../app
npm install
npm run dev                      # UI on :3001, proxies /api → :8181
npm run typecheck && npm run lint && npm run build
```

Ports are deliberately clear of the existing orchestra (OCR app 3000, SCB
receiver 8787, Streamlit 8501): engine **8181**, app dev **3001**.

## What it does

| Stage | Module | UI tab |
|---|---|---|
| Ingest `loadopoly.capture/1` bundles + loose JPEGs (EXIF/GPS, fixity) | `engine/stockopoly/intake` | Intake |
| Group photos: Brain knowledge → offline heuristics → optional LLM vision | `…/grouping` | Groups |
| Relational dimension solver (log-space WLS + sum-of-parts, Huber, CIs) | `…/dims` | Measure |
| Label grammar → 3D coordinates → Dijkstra travel costs | `…/locations` | Locations |
| CSV/XLSX imports (+ optional ERP bridge), supplier-dim crawler | `…/imports`, `…/partdims` | Inventory |
| ABC×XYZ velocity, occupancy, 3-scenario safety stock | `…/slotting` | Slotting |
| Greedy + swap optimizer, day-by-day migration tasks | `…/slotting` | Optimize |
| Instanced-bin 3D map (current / future / overlay) | `app/src/components/map3d` | Map 3D |

## Integration

- **Always-on learning** into the Supply-Chain-Brain `learning_log`
  (`engine/stockopoly/scb_link.py`); queues locally when the Brain is absent.
- The engine's **`/intake`** endpoint is wire-compatible with the Brain's
  photogrammetry receiver, so the Operate Console can uplink captures straight
  to StockOpoly with zero app changes.
- Optional **Supabase** sharing is toggleable and a clean no-op without config.

Raw photos and SQLite files live under `engine/data/` and are **never
committed**.
