# Contracts StockOpoly depends on

StockOpoly is the third sibling in the Loadopoly system. It **consumes** one
cross-repo contract and **borrows** two optional integration surfaces. None of
them are owned here — this doc records what we depend on and how to keep the
vendored copies in lockstep with their sources.

| Contract | Producer (source of truth) | StockOpoly consumer |
|---|---|---|
| `loadopoly.capture/1` bundle | `../Loadopoly-OCR/src/capture/types.ts` | `engine/stockopoly/intake/manifest.py` |
| Brain `learning_log` writer | `../VS Code/pipeline/src/photogrammetry/__init__.py` (or legacy `../Supply-Chain-Brain/…`) | `engine/stockopoly/scb_link.py` |
| Brain receiver wire format | `../VS Code/pipeline/src/photogrammetry/receiver.py` | `engine/stockopoly/server.py` (`/intake`) |
| Brain `data_access` (optional ERP) | `../VS Code/pipeline/src/brain/data_access.py` | `engine/stockopoly/imports/erp.py` |

## 1. `loadopoly.capture/1` bundle (consumed)

A bundle is a ZIP (or folder) of `scb_manifest.json` + `photos/IMG_*.jpg`.
The manifest's `schema` field is the literal `loadopoly.capture/1`. StockOpoly
vendors a **stdlib-only reader** of this manifest — it does not re-validate
every field the Brain does, only the structural invariants it relies on:

- top-level `schema` equals `SCHEMA_VERSION`;
- required keys `session`, `project`, `photos` present;
- `session.id` non-empty;
- each photo carries `file`, `sha256`, `pose`, `quality`, `width/height/bytes`.

**Field names must stay byte-compatible** with the producer. The pins live in
`engine/tests/test_manifest.py`:

- `test_schema_literal_pinned` — the literal string never drifts silently.
- `test_schema_matches_producer_types_ts` — when the `Loadopoly-OCR` sibling
  checkout is present, our literal must equal its `CAPTURE_SCHEMA_VERSION`.
- `test_schema_matches_brain_consumer` — likewise against the Brain's
  `SCHEMA_VERSION`.

These tests **skip** (not fail) when a sibling checkout is absent, so the
engine still tests green standalone, but they catch drift in any tree that has
all three repos side by side.

### Changing the contract

The contract is owned by the producer repos and changes there first:

1. Additive, optional fields → schema stays `loadopoly.capture/1`. Update the
   vendored reader only if StockOpoly needs the new field; no version bump.
2. Breaking change → producer bumps to `loadopoly.capture/2` and writes
   migration notes. Then, here: bump `SCHEMA_VERSION` in
   `engine/stockopoly/intake/manifest.py`, update the pins, and decide whether
   to accept both versions during a transition.
3. Run `python -m pytest engine/tests/test_manifest.py engine/tests/test_intake.py`
   and, in the Brain repo, `python orchestration/contract_check.py` +
   the `contract-guardian` subagent before committing on any side.

## 2. Brain `learning_log` (write target)

`scb_link.py` appends rows shaped
`(logged_at, kind, title, detail, signal_strength)` into the Brain's
`local_brain.sqlite`. This matches the table the Brain's photogrammetry intake
creates. We treat it as an **external-writer** surface: create-if-absent DDL,
WAL + busy-timeout, never schema-altering. If the Brain renames columns, update
`_LEARNING_DDL` and the insert in `scb_link.py`; the fallback JSONL mirror
(`scb_learning_outbox`-shaped) is StockOpoly-local and free to evolve.

## 3. Brain receiver wire format (`/intake`)

`server.py` exposes `/intake` so the Operate Console can uplink **here**
instead of the Brain with zero app changes. It mirrors the Brain receiver's
accepted shapes:

- `HEAD /` and `HEAD /intake` → 200 (reachability probe);
- `POST /intake` multipart with field `bundle` (ZIP) and optional `session`;
- `POST /intake` with a raw `application/zip` body.

If the Brain receiver's field names change, mirror them in
`_intake_bundle()`. Behaviour parity is exercised by
`engine/tests/test_server.py`.

## 4. Brain `data_access` (optional ERP bridge)

`imports/erp.py` imports `src.brain.data_access` from the Brain checkout only
when `STOCKOPOLY_ERP=1`. It calls `fetch_logical(connector, logical_name)` and
expects a pandas DataFrame (`.to_dict("records")`). This is the **one** place
allowed to pull in the Brain's heavier (pandas) stack; the engine core stays
stdlib-only. If `fetch_logical`'s signature changes, update `pull()`. The
bridge degrades to a clear `RuntimeError` when the env flag, the sibling
checkout, or the import is missing — never a hard dependency.

## 5. Shared Supabase project (soft integration surface)

Not a contract StockOpoly *consumes* — StockOpoly **owns** its remote schema —
but it shares one Supabase **project** and the OCR app's env-var names so the
hosted dashboard can point at the same backend with no new config:

- Env vars: `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` (app, browser) and
  `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`/`SUPABASE_ANON_KEY` (engine). These
  match `../Loadopoly-OCR/.env.example`.
- StockOpoly writes only **prefixed** objects: `stockopoly_*` tables and the
  `stockopoly-photos` Storage bucket, so it never collides with OCR's
  `historical_documents_global` / `corpus-images`. The DDL is shipped at
  `engine/sql/supabase_schema.sql` (idempotent; apply once).
- Producer/consumer of these tables both live **here**: producer
  `engine/stockopoly/supabase_sync.py` (`_TABLES`, `_PHOTOS_TABLE`), consumer
  `app/src/lib/cloud.ts`. Change them together and re-run the DDL.
