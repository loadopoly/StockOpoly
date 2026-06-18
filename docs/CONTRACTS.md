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
| Brain LLM ensemble (optional tier-3 vision) | `../VS Code/pipeline/src/brain/llm_ensemble.py` | `engine/stockopoly/grouping/scb_dispatch.py` |

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
expects a pandas DataFrame (`.to_dict("records")`). This is the only place
allowed to pull in the Brain's heavier **pandas** stack; the engine core stays
stdlib-only. (The tier-3 vision delegation in §6 also imports from the Brain but
is deliberately kept pandas-free.) If `fetch_logical`'s signature changes, update
`pull()`. The
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

## 6. Brain LLM ensemble (optional tier-3 vision delegation)

`grouping/scb_dispatch.py` borrows the Brain's vision-processing capability for
the tier-3 photo-grouping step. When the sibling checkout is importable it adds
`<scb>/pipeline/src` to `sys.path` and calls
`brain.llm_ensemble.llm_ensemble_call(messages, task="perception_visual")`,
handing the Brain the same OpenAI-style multimodal request the standalone route
(`grouping/llm.py`) would otherwise POST to OpenRouter. The Brain then owns model
selection (`llm_router` over its `config/brain.yaml` registry) and the
multi-provider caller (`llm_caller_openrouter`, with the xAI-Grok fallback), so
StockOpoly stops hardcoding a single vision model and instead routes the call
through the repo whose job that is. This is the **active** analogue of tier 1
(§ none — see `grouping/scb_vision.py`), which only *reads* what the Brain already
knows; here StockOpoly actively borrows the Brain's vision compute.

**Contract / invariants**

- *Entrypoint*: `llm_ensemble_call(messages: list, task: str) -> {"content": str, …}`.
  Messages are OpenAI chat-multimodal (`image_url` parts are forwarded verbatim
  by the Brain's caller); the reply's `content` is the model text.
- *Import weight*: this path must stay **pandas-free** — it is the lightweight
  counterpart to §4. `brain.llm_ensemble` only needs `db_path()` from
  `brain.local_store`, which now imports pandas lazily (inside its `fetch_*`
  helpers) for exactly this reason. If a future Brain change makes the ensemble
  import drag in the analytics stack again, the delegation simply stops
  activating in stdlib-first environments (it degrades, never errors) — so keep
  that import path light.
- *Degradation*: any failure — sibling absent, import error, offline/no-key
  sentinel, or unparseable reply — makes `scb_dispatch.propose` return `None`,
  and the cascade falls back to the standalone OpenRouter call. Gated by the
  `llm_vision` setting (tier 3 on) **and** `llm_vision_prefer_scb` (default on).
- *Task profile*: `perception_visual` is an existing vision-weighted profile in
  `config/brain.yaml`; unknown task names fall back to the router's `default`
  profile, so a renamed profile degrades rather than breaks.

Exercised by `engine/tests/test_grouping_dispatch.py` (real on-disk fake-Brain
import + fallback semantics). Standalone trees simply take the direct route.
