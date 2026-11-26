# Release Notes

## v0.30.0 - 2026-11-25

**Dual-Speed Cyber-Physical Nervous System, 7th Chain (`Return`) & Hybrid Outbox Relay (`x_OSQ v2.5.0`)**

Unifies HubCore (`:8000`), `HolonHubLive` (`:8900`), `Stockopoly` (`:8181`), `QUIPU`, M365 Teams Adaptive Cards, Rust RFID, and analytical Parquet/DuckDB export into a dual-speed cyber-physical control loop over a single-writer 4-tier data plane:

### Highlights & Cross-System Contracts
- **Afferent Sensory & Spatial Ingress (`annealing.py`, `spatial.py`, `geospatial_relation.py`):** Bound `space` axis `quipuSource` to `"stockopoly"`, added `SPATIAL_TELEMETRY_SOURCES`, extended `distill()` with `spatialFrame`/`rfidEdge`/`sourceHint` while preserving `"hubcore"` default, and added `IntakeForwarder.enqueue_distilled_telemetry()`. Added `sigma_pct_to_precision()`, `SpatialBridge.anchor_stockopoly_slot()` on `ENU_METER_WORLD_V1`, attached `spatialAnchor` to `stockopoly_slot_lookup()`, and added `register_stockopoly_slot_anchor()`.
- **7th Chain (`Return`) & Hybrid Outbox-to-HubCore Relay (`hub_modules.json`, `hub_workspace.json`, `bus.py`, `dataplane.py`):** Registered `return-quarantine-governance` on `chainId: "return"`, bumped `osq-forecast-governance` to `2.5.0` (`pathway.bifurcation`, `efferent.directives`), and declared `returnQuarantineGovernance` + `cyberPhysicalPathwayBus` datasets. Added durable `outbox_relay_cursor.json`, `get_relay_cursor()`, `set_relay_cursor()`, `relay_outbox_events()`, `pull_from_owner()`, and `status()` to `services/hubcore/bus.py`, plus `export_watermarked_parquet()` in `services/hubcore/dataplane.py`.
- **Pathway Bifurcation, Efferent Directive Router & M365 Tiering (`pathway_bridge.py`, `osq/engine.py`, `osq/server.py`, `hubcore/server.py`, `hubcore/authz.py`, `directionality_listener.py`):**
  - Added stdlib-only `CyberPhysicalPathwayRouter` (`classify_and_route`, `auto_close_directives_from_physical_event`, `sync_to_sqlite_brain`) across Reflexive Fast Path, Governed Owner Path, Analytical Export Path, and M365 Human-in-the-Loop Escalation Path (`IMMEDIATE_CARD` `>= 0.85`, `SHIFT_DIGEST` `0.65..0.85`, `LOG_ONLY` `< 0.65`).
  - Exposed `GET /api/v1/osq/{outbox,pathways,directives}` and `POST /api/v1/osq/directives/feedback` on `HolonHubLive` (`holonVersion: "2.5.0"`), wired physical-scan auto-closure on `rfid.event` (`RECEIPT_SCAN`) and `sap.signal` (`PICK_COMPLETE_KNOCKOFF`), non-blocking async push on commit (`HUBCORE_RELAY_URL`), and `record_holon_reuptake_pulse()`.
  - Exposed governed HubCore routes with `ROUTE_POLICY`: `GET /api/v1/bus/status` (`Requester`), `GET /api/v1/directives` (`Requester`), `POST /api/v1/bus/relay` (`WorkerOperator`), `POST /api/v1/directives/feedback` (`WorkerOperator`), and `POST /api/v1/dataplane/watermark` (`Reviewer`).
- **DeepWork Holon & Portable Child Suite Parity:** Bumped DeepWork `holon.json` (`2.5.0`, `holon-mesh/2.5`), updated `SKILL.md` and `moe_experts.json` (`1.2.0`), rebuilt `HubCore.zip` (`v1.0.23`), synced `Start-HolonHubLive.ps1` / `ops/Start-HolonHubLive.ps1`, and updated `HDS-CPQ-OSQ-Suite-v2.2.0` (`Start-Suite.ps1`, `Verify-Suite.ps1`, `openapi.json`, `Build-HdsProductRelease.ps1`).

---

## v0.29.0 - 2026-11-24

**`HolonHubLive` (:8900), TX03/TX77 Physical Inventory Marketplace, & Stockopoly Spatial Bridge**

Renamed `OsqLiveNetwork` to `HolonHubLive` across Holon configurations, browser channels (`x_holonhub_live_v2`), ephemeral store (`holonhub_live_temp.json`), HTTP/SSE server (`HolonHubLiveHttpServer` / `ops/Start-HolonHubLive.ps1`), and child Suite runtime, while incorporating the full TX03/TX77 Kitting physical fulfillment architecture:

### Highlights & Cross-System Contracts
- **Direct SAP API Time-Phased QOH & Snowflake Analytical-Only Role:** `GET /api/v1/osq/inventory/time-phased` projects `availableAtDate` `[T+0..T+14]` directly from physical SAP floor QOH minus committed reservations and storage `916`, including in-transit STO ETAs and excluding past-due `CO09` POs; `snowflakeRole` is locked to `analytical-export-only` (`transform_blueyonder.py`, `sync_connectors.py`, `services/osq/engine.py`).
- **Unified Physical Inventory Marketplace & Weighted Greedy Dijkstra:** `GET /api/v1/osq/marketplace/inventory` routes stock across 25 physical DCs weighted by traffic/population density and consolidation cost differential (`UNIFIED_MARKETPLACE_DIJKSTRA_DROPSHIP`); virtual storage bins (`TX03-in-TX77`, `NJ22-in-PA50`) are prohibited (`HTTP 400`, enforced in `services/osq/engine.py` and `services/marketplace/engine.py`).
- **Probabilistic Stockout Guardrails & Split Quote Lifecycle:** Computes `2σ@10d` / `3σ@6d` (`<0.27%`) expedite surge stockout bounds, 10-day Supervisor/CSR/Buyer warnings (minimum 5-day CSR customer notice), CTO scoring (`CTO_ROVING_STOCKPILE`, `SUPPLIER_DROPSHIP_SLA`, `CONSOLIDATED_HOLDING_COST_UPCHARGE`), SHA-256 `quote.split_allocate`, Modular BOM flattening (`pipeline/src/brain/multi_org_bom.py`), **Same-Day Duplicate STO `409` Guard** (`sto.schedule`), and in-app credit-hold RDD adjustment (`order.credit_hold_rdd_adjust`).
- **IFR Auto-PO, Receiving Quarantine & Rust RFID Nodal Map:** Automates replenishment POs (`/api/v1/osq/ifr/auto-po`), enforces Cancel-to-PO-Reduction Receiving Quarantine (`/api/v1/osq/ifr/cancel-reconcile`), tracks `rust-rfid-open-tracker/v1` `IS-*`/`CS-*` scans (`/api/v1/osq/rfid/event`) updating Dijkstra EMA edge weights (`alpha=0.25`), and renders `GET /api/v1/osq/logistics/nodal-map`, `IS`/`CS` chat channels, and Printable Floor Kanban.
- **QUIPU Note Classifier, Stockopoly Bridge & Bounded SAP Signal Guard:** Added `classify_handwritten_note()` (`/api/v1/osq/quipu/classify-note`), `SpatialBridge.stockopoly_slot_lookup()` + `POST /api/v1/spatial/stockopoly-slot` (`Requester` role in `services/hubcore/authz.py`) + `/api/v1/osq/stockopoly/slot-lookup`, and bounded SAP UI bridge `/api/v1/osq/sap/signal` (**blocking `RAW_INVENTORY_ADJUST` with `HTTP 403`**).
- **Documentation & Staging:** Updated `docs/APP_RECREATION.md` (Part 23.1 + Part 24), `docs/HUB_APP_RECREATION.md` (Part 11 + Part 13), `Loadopoly-Portal/index.html`, and staged `PROD/Hub_v10.html`, `UAT/Hub_v10.html` + `.sha256`.

---

## v0.28.0 - 2026-08-23

**V10-SEC-001 closed: every governed route requires a token, and the actor is the token**

v0.27.1 recorded that 59 of 67 routes took no token, and that release promotion,
held-result acceptance, the advisor Admin gate, and the v-ADMIN kill switch were
all reachable without one. This closes that.

### Authorization is a table, enforced once

`services/hubcore/authz.py` holds the policy keyed by `(method, path)`, and the
dispatcher applies it before any handler runs.

The previous shape is why the gap existed. Expressed as a `self._claims()` call
inside each handler, it was present on 8 routes and absent on 59, and **nothing
failed where it was missing** -- a new route was authenticated only if its author
remembered (`V10-SEC-002`). A served route with no policy entry now fails startup
rather than being reachable at whatever level the dispatcher defaults to
(`V10-SEC-003`).

The role ladder was already in the token broker and is now load-bearing:
`Viewer` < `Requester` < `ModuleAuthor` < `WorkerOperator` < `Reviewer` < `Admin`.

| Level | Reaches |
|---|---|
| public | The application, liveness, readiness, JWKS, session minting, the shared marketplace catalog, the scope routing table |
| `Requester` | Reads of governed state, data ingress, task creation, the advisor, hub-scoped marketplace routes within its own organization |
| `WorkerOperator` | Child hub attachment and proposal submission |
| `Reviewer` | Held-result acceptance and rejection, advisor review, grade override, send, close, bounded sessions, intake drain, spatial grounding, fleet status |
| `Admin` (r-ADMIN) | Release promotion, the advisor Admin gate, the kill switch, queue resolution, learning proposals, marketplace mint, trade, on-chain mint |

### The actor is the token, in 17 places

This is the half that mattered most. An acceptance previously recorded whatever
`reviewerId` the caller typed, and the Part 9.2 Admin gate was satisfied by any
non-synthetic string -- which made "direct Admin involvement" a claim rather than
a control.

`VirtualAdmin.assert_is_human` rejects a *synthetic* actor. It never established
that a human authenticated, and on its own it never could. That takes both parts:
the human-actor check and an `Admin` policy on the gate route (`V10-SEC-005`).

Verified live: a task created with `actorId: "IMPERSONATED"` in the body was
recorded as `r-admin`, the token's subject.

### A caller cannot mint itself authority

Requiring a token everywhere is vacuous if anyone can mint an Admin one.
`Requester` stays self-service and is the accountability tier -- a subject, a
tenancy, and an expiry, with no authority attached. `Reviewer` and `Admin`
require `HUBCORE_OPERATOR_SECRET`, presented in `X-HubCore-Operator` at
session-mint time (`V10-SEC-004`). Compose refuses to start without it, rather
than defaulting to a shared literal.

### The Hub UI

The generated application mints a `Requester` on load and sends it on every call.
Privileged actions prompt for the operator credential at the moment they are
taken, so an unattended browser does not sit holding r-ADMIN authority. The
header shows the role the session actually holds, and the reviewer and admin id
prompts are gone -- there is no id to type when the token already says who you
are.

One defect found and fixed in passing: the session badge read "no session" while
a token was in hand, because `ensureToken` set the role without refreshing the
badge. Same class of defect as a health panel reporting a state it never
measured, in a release specifically about not doing that.

### Documentation

The Part 23.1 route table now carries the real required role for all 67 routes,
generated from the policy table, and a check confirms every documented row agrees
with what the code enforces -- a route table that drifts from the policy is the
same defect in prose. Part 23.2 was rewritten from "known gap" to the policy that
closes it. `HUB_APP_RECREATION.md` gains Part 12 on obtaining and using a token,
including which status codes are retryable: `401` means mint and retry once,
`403` means stop (`V10-CLI-017` through `V10-CLI-019`).

### Verification

315 contract tests pass, up from 282, including a new
`tests/hubcore/test_authorization.py`. Live checks: anonymous reads and writes of
governed routes `401`; `Requester` reads `200` and privileged writes `403`;
self-minting `Admin` `401`; minting `Admin` with the credential `201`; and the
impersonation attempt above. The Hub loads clean under authentication with 4/4
required endpoints healthy.

### Upgrading

Set `HUBCORE_OPERATOR_SECRET` before starting. Any client or script calling
HubCore now needs a token: `POST /api/v1/session` for a `Requester`, and the
operator credential for anything higher. Scripts that previously called
`release/promote` or `tasks/accept` anonymously will get `401` until they mint one.

## v0.27.1 - 2026-08-23

**Specification audit: the served surface, and an authentication gap**

Documentation only; no behaviour changed. Both specifications were audited
against what the node actually serves, and three kinds of gap were found.

### The route surface is now documented

28 of the 67 served routes appeared in neither specification. `APP_RECREATION.md`
Part 23 now lists all of them, generated from `services/hubcore/server.py`, and
`HUB_APP_RECREATION.md` Part 11 gives the client-facing subset.

### Ten requirement identifiers had no definition

`V10-TSK-001`, `V10-ADV-001`, `V10-EXP-001`, `V10-GEN-001`, `V10-DAT-001`,
`V10-REL-001`, `V10-BUS-001`, `V10-COM-001`, `V10-PRV-001`, and `V10-TUN-001`
were cited by modules as though normative while this document had never stated
them. A citation nobody can look up is not a requirement. Defined in Part 0.3.2.

### Known gap recorded: authentication coverage

Auditing the auth requirement of each route surfaced something worth stating
plainly rather than papering over. Part 1.3 says every HubCore command is
"authenticated, authorized, schema-validated, and audited". Schema validation and
audit hold. **Authentication does not**: only 8 of 67 routes require a token --
the endpoint manifest, fleet status, data ingress, and the six hub-scoped
marketplace routes added in v0.26.0.

Everything else is open to anything that can reach the air-lock. The exposure is
bounded -- the air-lock publishes on `127.0.0.1` only, so that means any process
on the host rather than the network -- but it undermines controls this
specification states elsewhere:

- `POST /api/v1/release/promote` promotes to PROD with a caller-supplied actor id.
- `POST /api/v1/tasks/accept` and `/tasks/reject` are reviewer decisions.
- `POST /api/v1/advisor/admin-review` satisfies the Part 9.2 Admin gate with any
  non-synthetic string. `assert_is_human` rejects a *synthetic* actor; it does
  not establish that a human authenticated. This is a gap in the v0.24.0 advisor
  work, not in anything inherited.
- `POST /api/v1/vadmin/kill-switch` is a human decision per Part 10.4.
- `POST /api/v1/marketplace/trade` and `/web3/mint` are value-bearing.
- `POST /api/v1/support/ticket` accepts a caller-supplied sender address.

Recorded as `V10-SEC-001`, marked **not met as of v0.27.0**, with the hub-scoped
marketplace routes named as the reference pattern to generalise. The
specification now says explicitly that it must not be read as describing the
implementation's authentication posture until this is closed.

### Also documented

- StockOpoly and GARD Marketplace added to the Part 1.1 product surfaces table
  and the Part 11.2.1 endpoint registry, which still described StockOpoly as a
  host venv and omitted the marketplace entirely.
- `POST /api/v1/support/ticket`, the Customer Service Specialist ingress that
  grades inbound messages through the Part 9 pipeline (Part 23.3).
- **r-ADMIN** defined (Part 23.4). The term appeared once in a code docstring and
  nowhere else: it is the *real* human administrator, as against v-ADMIN the
  disclosed synthetic one. `V10-VAD-007` requires a v-ADMIN record to name
  r-ADMIN as the deciding authority.
- `V10-CLI-015` and `V10-CLI-016`: a client must send its token on every route
  that accepts one, including those the node does not yet require it on, and must
  not set a sender address it has not verified.

282 contract tests pass, unchanged.

## v0.27.0 - 2026-08-23

**The marketplace data plane: analytical tier, blob store, and one owner**

The marketplace claimed "atomic SQLite WAL transactional store and DuckDB Parquet
audit trails". Only the first clause was implemented: `data_dir` was created and
never written to, the directory held zero files, and DuckDB was not installed in
either image. This release builds the second tier and resolves who owns the store.

### The tier split, by mutability

SQLite was the right choice for half of it. Counted by write pattern,
`tokenized_assets`, `shard_holdings`, and `pending_rewards` are updated in place
-- a shard balance is a read-modify-write under concurrency, and a decrement
against immutable columnar storage means rewriting a file and hoping nobody
interleaved. Those stay in SQLite WAL.

`royalty_transactions`, `hub_floor_sync_records`, and envelope metadata never
update: 0 UPDATE/DELETE sites between them. They now live in Hive-partitioned
Parquet under `Data/parquet/marketplace/`, queried through DuckDB.

- `services/marketplace/lakehouse.py` - Parquet sinks, DuckDB reader, blob store,
  and a merge-only manifest.
- Rows land as newline-delimited JSON in their partition and are compacted into
  ZSTD Parquet. The analytical view unions pending shards with compacted files,
  so a record is readable the moment it is accepted: an audit trail that does not
  show the transaction you just made is the one freshness gap that is not
  acceptable (`V10-DAT-005`).
- Existing rows migrate out of the transactional store with an explicit,
  idempotent operation. Moving an audit trail is not a side effect of an import.

### The blob store

A GARD envelope carries base64 shard ciphertext inline. Measured: a 10 KB
plaintext produced a 2 KB envelope with four shards, and that scales with the
artifact. Envelope payloads now go to a content-addressed blob store and the row
keeps a SHA-256 digest. Content addressing means two writers storing identical
bytes converge on one file rather than racing.

### Fixed - the mocked analytical result

`dataplane.query_parquet` swallowed `ImportError` and returned
`[{"warning": "duckdb not installed; analytical parquet execution mocked"}]` -- a
result set shaped exactly like an answer, which a caller iterating rows cannot
distinguish from a measurement. It now refuses and names the missing dependency
(`V10-DAT-003`). DuckDB is installed in both images.

### Fixed - two writers to one store

The marketplace was declared twice: embedded in-process in `loadopoly-hubcore`
and as a standalone `gard-marketplace` service, both pointed at the same
`marketplace.sqlite` through the same host bind mount. It was latent only because
the standalone service had never been started.

The SQLite hazard was measured rather than assumed: two containers, 200 writes
each across the bind mount, 400/400 rows with `integrity_check` clean. SQLite WAL
survives it. **Parquet does not** -- no locking at all -- so the lakehouse
asserts single ownership at startup and refuses a second writer (`V10-DAT-004`).

Ownership is now explicit: HubCore is the centralization and policy layer;
`gard-marketplace` is the central location for Web3 shard and NFT records and the
sole writer (`V10-MKT-006`).

- HubCore's `/api/v1/marketplace/*` routes are an authenticated proxy. Scope is
  validated at the policy layer, so a typo never costs a round trip.
- The store moved from `ports:` to `expose:`. It is not published to the host: a
  loopback publish would let any process on the machine bypass the air-lock and
  read another organization's hub catalog (`V10-MKT-007`).
- On the bridge the store cannot see bearer tokens, so hub scope requires
  HubCore's resolved organization header plus a shared internal credential, and
  the organization is never re-derived from the request body (`V10-MKT-008`).
- An unreachable store is `503` naming the store. No cached or seeded catalog is
  substituted (`V10-MKT-009`).

### Added - the scope routing split

The scope split is which family of applications the information reaches, not a
label on a row, and it is now inspectable at `GET /api/v1/marketplace/routing`:
`shared` drives Loadopoly-OCR and Bakugo on the vision and touch axes; `hub`
drives Hub Floor and the organization on the entirety axis and never leaves it
(`V10-MKT-010`).

### Also

- `lakehouse._json_safe` coerces `datetime`, `date`, and `Decimal` at the source.
  Leaving them raw turned a successful query into a serialisation error at the
  HTTP edge, far from the code that caused it (`V10-DAT-006`).
- A `.dockerignore` was added. Without it the build context was the whole
  workspace, which failed outright on files the daemon cannot read.
- `.env.example` documents the internal credential; compose refuses to start
  without it rather than defaulting to a shared literal.

### Verification

282 contract tests pass, up from 267, including a new
`tests/hubcore/test_lakehouse.py`. Verified on the live stack: `:8600`
unreachable from the host, the public catalog served shared-only through the
air-lock, `?scope=all` refused, hub scope gated by token and organization, a hub
sync and an envelope writing real partitions and a real blob, compaction
producing Parquet, and DuckDB answering a GROUP BY across them.

## v0.26.0 - 2026-08-23

**StockOpoly becomes the space axis; the marketplace hub scope is confined**

Two things landed in the workspace: StockOpoly, and an Org Hub Marketplace
reported as implemented and verified. StockOpoly checked out. The marketplace was
mostly real and carried three ways to read another organization's internal
supply-chain records.

### Fixed - tenant confinement in the marketplace

- **The hard split failed open.** `get_catalog` applied the scope filter only
  when the value was recognised, so `?scope=all`, `?scope=`, or any typo dropped
  it entirely and returned every organization's internal hub assets beside the
  public catalog. Reproduced directly against the engine. The split now fails
  closed: an unrecognised scope is refused (`V10-MKT-001`).
- **Case bypassed confinement.** The filter lower-cased the scope while the
  decision to confine by `org_id` compared the raw string, so `?scope=HUB` chose
  hub assets and skipped the tenant clause. An `org-rival` inventory record came
  back from an `org-acme` query. Normalisation now happens once and every
  following decision uses it (`V10-MKT-002`).
- **No marketplace route authenticated.** Not one handler called `_claims()`.
  Forecasting models, warehouse lots, procurement SLAs, and cost-centre
  allocations were readable, mintable, transferable, and syncable with no token,
  and the organization was taken from the request body with a default of the
  local org. Every `hub`-scoped route now requires a token and takes the
  organization from it (`V10-MKT-004`).
- **A `hub` read must name an organization.** There is no cross-organization hub
  listing; an asset with no `org_id` is shared infrastructure, never another
  tenant's record (`V10-MKT-003`).

Verified live: `?scope=all` → 400, hub scope without a token → 401, another
organization's hub with a valid token → 403, public catalog → 200 shared-only.

### Fixed - the marketplace was 503 on the running node

Every test passed while the live service returned `503 marketplace engine
unavailable`. `_init_db` uses `CREATE TABLE IF NOT EXISTS`, which does not add
columns to an existing table, so a `marketplace.sqlite` written before the
hub-scope columns kept its old shape and every query naming `marketplace_scope`
failed. Tests used a fresh temp database and could not have seen it. The
initialisation error was also swallowed by a bare `except Exception`, so the 503
carried no reason.

- An explicit column migration now runs **before** index creation, because
  indexes over the new columns cannot be built on the old shape (`V10-DAT-002`).
- A failed subsystem initialisation names its cause in the response body
  (`V10-MKT-005`).

### Added - StockOpoly as the `space` axis

`StockOpoly/` is present and real: a stdlib-first warehouse engine doing
photo-derived dimension solving, a 3D location space with travel costs, and
current-to-future-state slotting. v0.25.0 recorded the spatial claims as
unimplemented, and at the time they were; the ideas were real and unpushed, not
invented, and the record is corrected rather than quietly overwritten.

- `Dockerfile.stockopoly` and a `stockopoly` service in the single compose stack,
  on `hideout_fleet`, published on loopback `:8181`.
- `hub_workspace.json` `warehouseEngine` gains `serviceHost: stockopoly` and
  `axis: space`. The workspace schema's axis enum only ever listed three of the
  six axes; it now covers all of them.
- The axis registry reports `space` implemented and live. The same rule that
  reported it `reserved` last release reports it `live` now.

### Added - the Touch-to-Space metrology bridge

The Bakugo-to-warehouse dimensional loop that was described before it existed.

- Bakugo gained `GET /slabs`, publishing measured slab metrology: thickness, its
  standard deviation, and the refractive index it was derived through. It
  previously published holder *labels* only, which is why nothing could consume a
  dimension.
- `services/hubcore/spatial.py` registers those constants as StockOpoly
  `mesh/touch/<slab>` reference objects, with the measurement's own error bar as
  `sigma_pct`.
- Dimensions are read from the Touch axis, never supplied by HubCore: an
  invented metrology figure is indistinguishable from a measured one
  (`V10-SPA-001`). Uncertainty travels with the value (`V10-SPA-002`). Grounding
  is idempotent (`V10-SPA-003`). The bridge is advisory — it grounds a solve, it
  does not move stock or approve an allocation (`V10-SPA-004`).
- Routes: `GET /api/v1/spatial`, `GET /api/v1/spatial/ground-truth`,
  `POST /api/v1/spatial/ground`.

Verified on the live stack: six slabs registered (`psa` 1.20 ±0.30 mm, `bgs`
1.60 ±0.40, `cgc` 1.40 ±0.40, `toploader` 0.55 ±0.15, `penny_sleeve` 0.05 ±0.02,
`case_glass` 5.00 ±1.50), `raw` skipped as having no holder thickness, and a
second run reporting six unchanged and zero registered.

### Documentation

- `APP_RECREATION.md`: section 11.2.7 for the Touch-to-Space bridge
  (`V10-SPA-001`-`005`), section 12.6 for marketplace scopes and hub confinement
  (`V10-MKT-001`-`005`, `V10-DAT-002`), and the `space` axis moved from reserved
  to implemented in the section 11.2.6 table.
- `HUB_APP_RECREATION.md`: Part 9 on consuming the marketplace from a client,
  including which routes need a token, that the organization comes from the token
  and never from a parameter, and what each refusal status means
  (`V10-CLI-009`-`011`).
- `HUBCORE_V10_INTEGRATION.md`: section 8 records the audit, claim by claim.

### Claims that did not hold

`walkthrough.md` does not exist anywhere in the workspace. The test count
(249 passed, 1 skipped) matched exactly at the time it was reported.

### Verification

266 contract tests pass (up from 249), including new
`tests/hubcore/test_spatial_bridge.py` and leak regressions in
`test_gard_marketplace.py` and `test_browser_contract.py`. Six of six declared
axes are implemented and five are live; `scb-brain` is down because the Supply
Chain Brain is not running on this host.

## v0.25.0 - 2026-08-23

**Closing the annealing seam, and correcting the record on the cognitive mesh**

A description of the QUIPU / Supply Chain Brain / Bakugo mesh was circulating that
mixed verified behaviour with components nobody had built. Auditing it against the
codebase turned up a real break at the one seam HubCore owns.

### Fixed - the intake queue was a dead end

- **Nothing drained the training intake queue.** The air-lock screened external
  submissions and appended them to `Data/hub/inbox/quipu_training_intake.jsonl`,
  and no code anywhere read that file. QUIPU had a `hubcore` source profile
  registered on the `perception` axis, waiting for observations that could not
  arrive. `services/hubcore/annealing.py` now drains the queue into the
  observer's `/observe` route (`V10-MESH-002`).
- **The queue held nothing trainable.** The record carried only receipts -
  content hash, consent tier, gate report - because screening discarded the
  payload. `airlock.summarize_payload` now distils the payload's *shape* while it
  is still in hand: field count, and a vector's dimension, L2 norm, mean, and
  extrema.
- **Distillation, never replay.** What crosses the seam is a bounded observation
  sentence plus summary statistics. No submitted field value survives, so the
  annealing loop cannot become a bypass around the membrane (`V10-MESH-003`).
- **Fail-safe cursor.** The forwarder advances only over settled records. An
  unreachable observer stops the batch where it stands, leaves the queue intact,
  and reports the lag; a restart resumes at the same record rather than
  re-observing one already counted (`V10-MESH-004`).
- **An empty queue now explains itself.** This node runs at consent tier 0, which
  accepts no artifact kind, so external ingress is refused at the membrane. An
  idle loop there means the door is shut, not that clients are quiet - and the
  status says which (`V10-MESH-005`).

### Added - the axis registry

- `annealing.AxisRegistry` reports the sense-axes the node actually has. An axis
  is `live` only when an implementation exists **and** its endpoint answered;
  declared-and-down is `down`, declared-and-unbuilt is `reserved` (`V10-MESH-001`).
- The `space` axis (`stockopoly`, warehouse spatial allocation) is reported
  `reserved` and carries a note saying no implementation exists. A healthy probe
  would not make a spatial engine exist.
- New routes `GET /api/v1/mesh/axes`, `GET /api/v1/annealing`, and
  `POST /api/v1/annealing/drain`; both are folded into `/api/v1/status`.
- A background loop drains the queue every 45 s alongside the service.
- New Hub view, **Cognitive Mesh**: the axes with their real status, the seam's
  actual throughput, and an explicit banner when the membrane is closed.

### Documentation - what is true and what was asserted

`docs/HUBCORE_V10_INTEGRATION.md` section 7 records the audit.

Verified present: Floquet periodic annealing cycles (`QUIPU_TRAIN_INTERVAL: 45`,
`pipeline/src/brain/`), resonance-gated capability generation
(`RESONANCE_FLOOR_HZ = 52.0`, `tool_forge.py:83`), continuous-field state via
`weyl_centroid` and `relational_gradient` in `temporal_spatiality.py` ported into
Bakugo, five source-routed sense-axes in `observer_service.py`, the closed
Touch-Vision loop through `/observe`, `/feedback`, `/guidance` and
`numeric_lexicon`, Snell-law optical metrology in `Bakugo/cardcenter/optics.py`,
and an Almgren-Chriss execution model - which belongs to **Bakugo**, for card
liquidation.

Verified absent: any StockOpoly implementation, a relational-to-Euclidean
warehouse embedding, Bakugo-to-StockOpoly packing factors, a StockOpoly-to-SCB
liquidation path, a warehouse slotting engine, and a Customer Service Specialist
Agent. Warehouse slotting appears only as research query strings and ground-truth
training topics.

`docs/APP_RECREATION.md` gains section 11.2.6 with the axis table and
`V10-MESH-001` through `V10-MESH-005`, and states plainly that the `space` axis is
a reservation of a port and a name.

`docs/HUB_APP_RECREATION.md` gains section 4.1.3 telling client developers exactly
what survives ingress and what does not, plus `V10-CLI-008`.

### Verification

244 contract tests pass (up from 225), including a new
`tests/hubcore/test_annealing_seam.py` covering distillation, non-replay,
deferral without loss, cursor durability, corrupt-line tolerance, and the
reservation rule. The seam was also proved end-to-end against the live QUIPU
container: a distilled observation was accepted onto the `perception` axis.

### Not changed, deliberately

The node's consent tier stays at 0 and federation stays disabled. Opening a tier
is the `{{dataRightsHolder}}`'s decision under Part 20.3, not an implementation
detail, so the membrane was left shut and the seam was proved with a synthetic
record instead.

The `space` axis remains unimplemented. Building a warehouse spatial-grounding
engine is a product, not a fix, and inventing one in documentation is the exact
failure this release exists to correct.

## v0.24.0 - 2026-08-23

**HubCore v10.1: the Hub becomes a working application, plus the Part 9 advisor and v-ADMIN**

v0.23.0 delivered the foundation but not a usable product. The generated Hub could
not reach HubCore at all, and where it failed it printed a confident fake success.
This release makes every surface report only what it measured, adds the Part 9
advisor pipeline and the Part 10.4 virtual Admin, and consolidates the node into a
single compose stack.

### Fixed - surfaces that reported states they had not measured

- **The Hub could never reach the authority.** HubCore sent no CORS headers and had
  no `OPTIONS` handler, so the browser discarded every response before HubCore saw
  it and every `POST` preflight returned `501`. Added an allow-listed origin echo
  (loopback hosts and the `null` origin an Electron shell reports) and a preflight
  handler. A foreign origin still receives no header (`V10-END-004`).
- **Fabricated success on failure.** The fleet panel printed "Fleet Active & Bound
  to 127.0.0.1 - Direct probes verified" *on a failed fetch*, and ignored the
  response body on a successful one. The advisor invented "Sovereign response:
  query processed locally". The child-hub view seeded three fictional hubs marked
  `active`. The RDP terminal echoed `[SANDBOX-EXEC] Command executed` while
  executing nothing. All removed; a failed call now names the endpoint and the
  error (`V10-UI-001`, `V10-UI-002`).
- **Unresolved brand tokens rendered to screen.** `hub_org.json` still held literal
  `{{hubName}}`/`{{orgName}}` placeholders. Identity is configured, generation
  fails closed on an unresolved token, and `doctor` reports any that remain.
- **Task state was lost on restart.** `TaskManager` was in-memory and unsynchronized
  behind a threading HTTP server. Now SQLite WAL with an append-only transition
  audit and a lock; a held external result survives a restart.
- **Readiness took 6.5 s.** Endpoint probes ran serially, so `/health/ready` cost
  the sum of every endpoint's latency and the UI spinner outlived the browser's
  patience. Probes now fan out and results are cached for 10 s.
- **Containerized HubCore reported a healthy fleet as unreachable.** Inside a
  container `127.0.0.1` is the container. Descriptors now declare `serviceHost`
  and probing follows where HubCore runs (`V10-END-002`).
- **Federation reported itself enabled from a transport artifact** rather than from
  the org-level consent decision in `hub_org.json`.

### Added - Part 9 advisor pipeline

- **`corpus.py`** - dependency-free BM25 index over the governed sources: the
  specification, operator documentation, and the hub configuration rendered as
  citable prose. Every passage carries source, section, and a content hash of the
  exact text read. Retrieval that matches nothing returns nothing (`V10-ADV-005`).
- **`advisor.py`** - the Part 9.2 grade matrix (0-5) with non-decreasing Expert
  weights, a transparent grader that publishes the signals behind every grade, a
  confidence floor that escalates rather than guessing (`V10-ADV-003`), reviewer
  and Admin gates, grade overrides recording old grade/new grade/actor/rationale,
  Part 9.4 follow-up classification with a versioned calendar policy, and recorded
  closure. Grade 5 cannot close without a recorded Admin decision. A request with
  no retrieved evidence never reaches `READY_TO_SEND` (`V10-ADV-002`).
- Request state and the stage trail are durable in SQLite WAL.
- Instruction-shaped text in a request or a retrieved passage is quarantined,
  recorded, and stripped of authority; the gate the grade demands is unchanged
  (`V10-ADV-004`).

### Added - Part 10.4 v-ADMIN

- **`vadmin.py`** - the disclosed virtual Admin. Prepares the parallel Admin record
  grades 4 and 5 always produce, holds the queue of what awaits a human, and
  refuses every human-only decision by identity: approval, closure, promotion,
  acceptance of a held result, and its own kill switch (`V10-VAD-002`).
- Learning proposals naming an authority-bearing field are refused outright, by
  stem, so `addTaskKind` is caught alongside `taskKind` (`V10-VAD-003`). Accepted
  proposals are staged for independent evaluation, never applied online.
- The kill switch is global and fails safe; engaged, pending work falls back to
  the human queue and the gates are untouched (`V10-VAD-005`).
- The Admin queue is a view of durable state, rebuilt on startup from requests
  still awaiting an Admin (`V10-VAD-006`).

### Added - the served application

- HubCore serves the generated Hub at `GET /` from its own origin (`V10-END-003`).
- `GET /api/v1/status` - one bounded snapshot the dashboard renders directly, so
  no displayed number can drift from what HubCore measured.
- New routes: `/api/v1/chains`, `/api/v1/tasks/audit`, `/api/v1/tasks/reject`,
  the twelve `/api/v1/advisor/*` routes, and five `/api/v1/vadmin/*` routes.
- Hub views rebuilt as live clients: dashboard, Seven Chains, advisor with grade
  and gate and citations, advisor ledger with reviewer/Admin actions, fleet
  posture, Hub Floor task ledger with accept/reject/trail, v-ADMIN queue with a
  kill-switch control, child hubs, modules, and node configuration.

### Changed - one stack, one project, one network

- `docker-compose.yml` is now the single compose file for the node: project
  `hideout`, network `hideout_fleet`, all fleet members and every ingress tunnel
  (`V10-DEP-001`). `Loadopoly-OCR/docker-compose.yml` is marked superseded.
- Durable volumes are pinned by explicit external name, so the project rename
  carried the QUIPU brain and CardCenter database across intact (`V10-DEP-002`).
- Tunnels moved behind per-surface profiles, off by default, each named for the
  one surface it exposes (`V10-DEP-003`). The air-lock tunnel is distinguishable
  at a glance from the application tunnels.
- Child hubs: registry starts empty, decays to `stale` without heartbeats, refuses
  proposals and sessions from unattached hubs, and no longer puts a session token
  in a URL path.
- `Dockerfile.hubcore` no longer bakes `HUBCORE_ALLOW_WIDE_BIND=1`; binding the
  bridge from inside a container is the container-bridge case V10-END-001 already
  permits, and a wide bind elsewhere still fails closed.

### Documentation

- `docs/APP_RECREATION.md` - added the Part 0.3.1 implementation-status table
  (`V10-DOC-001`), section 1.5.1 single-stack deployment rules, the Part 9 route
  surface and state machine, section 9.5 retrieval and injection rules, section
  10.4.1 v-ADMIN, section 11.2 `bind` vs `serviceHost`, and section 11.2.5 browser
  access to the authority.
- `docs/HUB_APP_RECREATION.md` - the external client specification is now
  versioned in this repository. Corrected the ingress route (the served route is
  `POST /api/v1/ingress`, not `/api/v1/membrane/telemetry`), documented session
  minting and browser transport, added client obligations `V10-CLI-001` through
  `V10-CLI-007`, and Part 8 on consuming the advisor.
- `docs/HUBCORE_V10_INTEGRATION.md` - evidence index extended to the new
  requirement IDs, plus section 6 recording exactly what each surface used to claim.

### Verification

225 contract tests pass (up from 169). New files: `tests/hubcore/test_advisor_pipeline.py`
and `tests/hubcore/test_browser_contract.py`. Verified live against the running
node: 4/4 required endpoints healthy and V10-END-001 compliant, a full advisor
lifecycle from submission to recorded closure, v-ADMIN refusing a synthetic Admin
decision, and both the task ledger and the Admin queue surviving a container restart.

### Known limitations

- HubCore's container has no GPU passthrough, so the HAL reports Conductor Tier 0
  (cpu-only) for HubCore's own compute. That is accurate for what HubCore can use;
  inference runs in the fleet containers.
- The Part 9.4 follow-up and meeting policy is implemented and versioned, but no
  calendar is connected. Slot proposal is policy, not a booking.
- Expert distillation (Part 10.2) stages proposals; nothing is trained.

## v0.23.0 - 2026-08-22

**HubCore v10 Foundation: Sovereign Central Node, Tenant Isolation, and the Data-Only Air-Lock**

Integrates `docs/APP_RECREATION.md` (the v10 canonical specification) into this
repository and implements the foundation layer it requires on Black Bulls Hideout.

### Added

- **`hub/`** - the Part 2.1 configuration split: `hub_org.json` (identity, seven chain
  slots, federation consent tier), `hub_workspace.json` (library, hardware, endpoint
  registry, tunnels, air-lock, allocation spaces, data plane), `hub_modules.json`
  (module registry), and published JSON Schemas under `hub/schemas/`. All display
  identity is a `{{token}}`; no organization name, tenant URL, or secret is compiled in.
- **`services/hubcore/`** - the sovereign authority layer:
  - `hal.py` - Hardware Abstraction Layer. Discovers CPU/RAM/GPU topology via NVML,
    `nvidia-smi`, or torch and selects Conductor Tier 0-3. This host classifies as
    **Tier 1** (RTX 3060, 12 GB) with one heavy model resident and a 4 GB budget.
  - `allocations.py` - composite tenancy tuple `(orgId, siteId, deviceId, repoId, userId)`,
    subtree provisioning with WAL-mode SQLite seeding, per-call path confinement, quotas.
  - `token_broker.py` - RS256 scoped token minting on `cryptography` with no PyJWT
    dependency and no `alg=none` path. Release-authority capabilities are unmintable.
  - `airlock.py` - the Data-Only Ingress Air-Lock and ContaminationGate. Commercial
    terms are unencodable at the field name; consent tiers gate every artifact kind.
  - `endpoints.py` - endpoint registry, health probes, and V10-END-001 binding audit.
  - `statepack.py` - `.statepack` export/import with a hash-chained, optionally signed
    manifest; bit-exact across machine migration.
  - `server.py` - the `:8000` token broker and air-lock service, loopback-bound.
  - `cli.py` - operator CLI: `doctor`, `hal`, `health`, `audit-bindings`, `tenant`,
    `token`, `anneal`, `gate`.
- **`pipeline/src/brain/hubcore_bridge.py`** - the seam between the Supply Chain Brain
  and HubCore: endpoint resolution, HAL admission control, tenancy, egress screening.
- **`ops/`** - `Start-HubCore.ps1`, `Verify-V10-Compliance.ps1` (executable release gate),
  and `Repair-LoopbackBindings.ps1` (dry run unless `-Apply`).
- **`tests/hubcore/`** - 146 contract tests covering traversal and cross-tenant refusal,
  token forgery and expiry, every ContaminationGate category, tier classification across
  seven topologies, state pack integrity, and the live onboarding handshake.

### Changed

- **`pipeline/src/brain/tri_repo_feedback_loop.py`** - the hardcoded
  `_DEFAULT_QUIPU_URL` import-time constant became a lazy registry lookup, so a
  configuration change is picked up on the next loop. `QUIPU_URL` is still honoured.
  Added `assert_allocation_access()` for engine-level tenancy enforcement,
  `screen_egress()` for ContaminationGate screening, and `fleet_status()`.
  `verify_tenant_isolation()` is retained for existing callers but now documented as
  insufficient on its own under V10-ALC-001 - it compares labels, not real paths.
- **`VERSION`** - `0.22.97` -> `0.23.0`.

### Security

- Cross-tenant access now fails closed at the storage engine rather than by UI or
  string filtering. `allocations.confine()` re-resolves symlinks on every call, so a
  junction planted after provisioning still escapes nothing.
- The broker signing key is generated under `%APPDATA%/HubCore/keys` with owner-only
  permissions and is asserted by test to live outside the library root.
- `release.promote`, `uat.confirm`, `prod.write`, `archive.write`, `policy.write`, and
  `shell` cannot be minted into any tenant token.

### Infrastructure Rectification & Verification

- `loadopoly-ocr:3000` (Vision axis) investigated, rebound to `127.0.0.1:3000:3000`, set to `restart: unless-stopped`, and restarted healthy.
- `quipu` (7100) and `bakugo` (8765) rebound to `127.0.0.1` loopback in compose and via `ops/Repair-LoopbackBindings.ps1`, closing all V10-END-001 findings.
- The `:8000` HubCore token broker and air-lock is persistently running, with background execution supported via `.\ops\Start-HubCore.ps1 -Background` and watchdog monitoring in `pipeline/brain_watchdog.ps1`.
- The Supabase stack on `:54321`/`:54322` remains recorded as `legacyEndpoints` with status `deprecated-migrating-to-native-data-plane` for eventual migration to SQLite WAL + DuckDB Parquet.

### Verification

- `python -m pytest tests/hubcore -q` -> **145 passed, 1 skipped**
- `.\ops\Verify-V10-Compliance.ps1 -Strict` -> **All evaluated v10 gates PASS (0 failures, 0 binding findings)**
- `python -m services.hubcore.cli health` -> **All required endpoints healthy (quipu-observer, loadopoly-ocr, bakugo, hubcore-token-broker)**
- `python -m services.hubcore.cli audit-bindings` -> **0 findings, compliant: true**

See `docs/HUBCORE_V10_INTEGRATION.md` for the full baseline, gap analysis, and
requirement evidence index.

## v0.22.97 - 2026-06-01

**Density Expansion Unblock + First Verified Autonomous ACRE Patch**

### Changed

- **`pipeline/src/brain/system_entirety.py`** - optimized `_recent_learning_state()` by switching from function-wrapped datetime filtering to direct `logged_at >= ?` comparison and adding `ix_ll_loggedat` index creation. This removes the full-table-scan bottleneck that was stalling forced expansion rounds.
- **`pipeline/src/brain/code_restructure.py`** - added corpus-density signal scanning and a new `_gen_corpus_depth_tune()` patch generator. When corpus density exceeds baseline (40k entities / 400k edges), ACRE can now generate a high-certainty depth tune independent of forge-node median confidence.
- **Autonomous self-modification runtime outcome** - the new corpus-density path produced `citation_depth_tune_*` with `combined_certainty=1.0`, passed gate `0.82`, and applied a verified patch in `pipeline/src/brain/citation_chain_acquirer.py` (`_DEFAULT_MAX_DEPTH: 3 -> 4`) with commit `e5d2c9de`.

### Runtime Impact

- Forced 7th-D expansion no longer stalls in `_recent_learning_state` during heavy `learning_log` windows.
- Trust ledger bootstrap completed with the first verified autonomous apply outcome (`pipeline/src/brain/citation_chain_acquirer.py::param_tune`).
- Density-expansion telemetry improved during the round: vision axis advanced from ~0.21 to ~0.40 and mesh synchrony increased.

### Verification

- `pytest tests/test_self_modification.py tests/test_synaptic_workers.py -q` -> **24 passed**
- Forced density expansion + self-modification round completed end-to-end with:
  - `patches_evaluated=2`
  - `patches_applied=1`
  - `committed=true`

---

## v0.22.95 - 2026-05-29

**GitHub Actions Workflow Recovery for Daily Dispatch and Cloud Learning**

### Changed

- **`.github/workflows/daily_dispatch.yml`** — fixed the embedded Python heredoc indentation so the scheduled dispatch path can call `send_daily_dispatch()` again. The workflow now also checkpoints `pipeline/cloud_brain.sqlite` before cache save and caches the `-shm` / `-wal` sidecars together with the main SQLite file.
- **`.github/workflows/cloud_learning.yml`** — checkout now uses full history, the workflow checkpoints `pipeline/cloud_brain.sqlite` before saving the cache, caches the SQLite sidecars with the main DB, and rebases with retries before pushing `pipeline/cloud_learning_queue.jsonl` back to `main`.
- **`pipeline/tests/test_github_workflows.py`** — added focused workflow regression coverage so the embedded workflow Python blocks stay syntactically valid and the cloud-learning git/cache hardening stays pinned.

### Verification

- `pytest tests/test_github_workflows.py tests/test_daily_dispatch.py -k "not start_daily_dispatch_daemon"` → **8 passed, 1 deselected**
- Direct AST parse of all embedded Python heredocs in `.github/workflows/daily_dispatch.yml` and `.github/workflows/cloud_learning.yml` → **passed**

---

## v0.22.94 - 2026-05-29

**Internal Marketplace — 5-Stream Shadow Price Engine (Research Stream Removed)**

### Changed

- **`pipeline/src/brain/internal_marketplace.py`** — removed the External Return Channel Research evidence stream. Shadow price now blends **five** independent signals: hedonic regression (W=0.25), cross-site RFQ differential (W=0.20), commodity index drift (W=0.15), cannibalization transfer price (W=0.15), and peer-cluster anchor (W=0.25). Removed `ResearchDispositionSignal`, the `_RESEARCH_*` constants and keyword dictionaries, the WAL connection cache, and the `research_return_signal` / `research_confidence` / `recommended_channel` fields from `ShadowPrice`.
- **`pipeline/pages/25_Internal_Marketplace.py`** — updated shadow-price page to five-stream description; removed `research_return_signal`, `research_confidence`, and `recommended_channel` columns from the parts grid; removed "External Return Signal", "Recommended Channel", and "Research Confidence" metric tiles from the waterfall detail view; updated caption.
- **`_build_marketplace_deck.py`** — updated slide 1 and summary-slide version labels to `v0.22.94`; renamed Slide 3 to "Five Evidence Streams"; removed the Research Return stream card; and simplified the generated deck output path using `os.path`.

### Verification

- `python -m py_compile _build_marketplace_deck.py pipeline/pages/25_Internal_Marketplace.py pipeline/src/brain/internal_marketplace.py pipeline/src/brain/_version.py` → **passed**

---

## v0.22.62 - 2026-05-24

**Hideout Mesh Heartbeat Resuscitation, Stale Derived-Heartbeat Refresh, and Bit-Flip Interaction Map**

### Added

- **`pipeline/docs/HIDEOUT_MESH_INTERACTION_MAP.md`** — operator-facing interaction map tracing the live hideout activation path from `bridge_rdp.py` through `asset_resource_mesh_last.gpu_resuscitation` down to `bit_flip_resuscitation.bit_flip_nodes`, including the current synthetic hideout heartbeat contract and mesh-discovery surfaces.

- **Focused regression coverage in `pipeline/tests/test_bridge_rdp.py`** — added coverage for historical live-state derivation, derived heartbeat publication, stale derived heartbeat refresh, and stale derived heartbeat refresh when persisted resuscitation history is unavailable.

### Changed

- **`pipeline/bridge_rdp.py`** — `_hideout_resuscitation_activation()` now republishes historically derived hideout GPU state into `pipeline/bridge_state/compute_peers/scbrain-hideout.json`, refreshes stale synthetic hideout heartbeats, and preserves `all_expected_gpus_accessible` correctly in the published heartbeat metadata.

- **Hideout mesh rendezvous state** — the live `scbrain-hideout` heartbeat now materializes as a 12-GPU `devtunnel` peer in compute-grid discovery, sourced from persisted resuscitation until the physical hideout compute node overwrites it with a real heartbeat.

### Verification

- `pytest tests/test_bridge_rdp.py -q -p no:cacheprovider` → **21 passed**
- Live validation: `bridge_state/compute_peers/scbrain-hideout.json` refreshed with `derived_from_resuscitation: true`, `resuscitation_activation.all_expected_gpus_accessible: true`, and `discover_peers(force=True)` returned `scbrain-hideout` as a present `devtunnel` peer with 12 GPUs.

## v0.22.57 - 2026-05-21

**Repository Catalog Mesh Integration, Synaptic PIM Worker, WIP Aging Page, SCB Insight Echo Guard**

### Added

- **Mesh peer registry in `repository_catalog.py`** — `_mesh_runtime()` builds a live peer topology from `observer:peer_registry` KV entries with a 300-second liveness window. Enables shard-based catalog distribution: each peer host gets a `shard_index / num_shards` slice so large workspace scans don't duplicate work across the mesh.

- **Repository catalog as system-entirety signal** — `system_entirety.py` now persists `RepositoryCatalogState` as a first-class mesh entity with a `APPLIES_REPOSITORY_CATALOG` edge to the active learning window. Catalog signal (coverage, staleness, file count) contributes 18% weight to the overlay strength calculation via `_REPO_CATALOG_GAIN = 0.18`.

- **`_pim_planning_sync_worker` in `synaptic_workers.py`** — new 24-hour daemon worker that harvests planning parameters from all ERP sources (Oracle Fusion FSCM REST + BIP SQL, GSS St. Cloud inventory sub-ledger, AX Dynamics Airport Rd on-hand, Epicor Jerome/PDC cycle-count). Soft-skips on transient network errors to prevent backoff accumulation during Oracle outages.

- **`pipeline/pages/20_WIP_Aging_Review.py`** — WIP aging review page additions.

- **Regression coverage** — `tests/test_repository_catalog.py` (+130 lines) and `tests/test_system_entirety.py` (+181 lines) verify mesh runtime construction, peer liveness, shard assignment, catalog signal propagation, and system-entirety entity/edge upserts.

### Changed

- **`doc_rag.py` — SCB Insight echo guard** — `synthesize_plant_insight` now detects offline-caller echo responses (`^\[model_id\]` prefix pattern), retries dispatch after registering the OpenRouter caller, and returns `None` when no live inference is available. Prevents garbage dict reprs from appearing in plant email blurbs.

- **`_run_pi_q1_rollup.py` removed** — Q1 rollup functionality fully superseded by `_run_pi_periodic_rollup.py`; deleted to eliminate dead-code confusion.

- **`config/model_map.json`** — minor model routing adjustment.

### PI Output Artifacts (YE2025 vs Q1 2026)

- `03_Executive_Rollup_YE2025_vs_Q1_2026.pptx` — refreshed executive deck.
- `04_Plant_Email_Blurbs.md` — removed 12 malformed `📖 SCB Insight` lines containing raw `EnsembleResult` dict reprs generated before the echo guard was in place.
- `04_Plant_Emails_Formatted.html` — paired HTML refresh.
- `06_Data_Discrepancy_Report.md` — updated discrepancy report.

### Verification

- `py_compile src/brain/doc_rag.py src/brain/repository_catalog.py src/brain/system_entirety.py src/brain/synaptic_workers.py` → **CLEAN**
- `pytest tests/test_repository_catalog.py tests/test_system_entirety.py -q` → **PASSED**

---

## v0.22.56 - 2026-05-21

**Native ERP Determination, Canonical Site/System Mapping, and Audited Dispatch**

### Added

- **`pipeline/src/brain/site_system_map.py`** — canonical Astec plant registry for ERP, system, connector, business-unit, and database-platform mapping. Captures AX for Eugene Airport Rd, SyteLine for Parsons, Epicor for Jerome and Prairie du Chien, Oracle Fusion for Wilson Rd, Manufacturers Rd, and Burlington, Oracle Legacy for Blair with planned Oracle Fusion cutover in October 2026, and GSS for St. Cloud.

- **`pipeline/src/brain/erp_native.py`** — native ERP capability layer for SCB. Centralizes ERP signal detection, canonical aliases, site resolution, connector-candidate resolution, and the new hierarchical resolver: learned structure → site/system map → user impute → payload fallback.

- **`pipeline/src/brain/erp_integration.py`** — ERP payload inspection and dispatch orchestration. Adds preview/apply support for Oracle Fusion REST, SQL-based ERP updates, GSS file export, and durable local SQLite audit rows in `erp_integration_audit`.

- **Focused regression coverage** — added `pipeline/tests/test_erp_native.py` and `pipeline/tests/test_erp_integration.py` to verify native ERP detection, canonical site assignments, learned-first ERP resolution, dispatch behavior, and audit logging.

### Changed

- **Repository routing and PIM synthesis** — `pipeline/src/brain/repository_catalog.py` and `pipeline/src/brain/pim_part_synthesis.py` now use the shared native ERP layer instead of local ERP heuristics, so routing metadata, per-part payload synthesis, and integration dispatch stay aligned.

- **Mapping unification** — `pipeline/src/deck/erp_translation.py`, `pipeline/src/brain/pim_planning_sync.py`, and `pipeline/src/deck/live.py` now consume the canonical site/system map instead of maintaining drift-prone plant mappings and manual business-unit fallbacks in multiple places.

- **Connectors page** — `pipeline/pages/6_Connectors.py` now exposes the site/system/database mapping table, native ERP detection panel, learned-first payload inspection, live dispatch controls, and the recent ERP integration audit trail.

- **User ERP input behavior** — the manual ERP selector is now a fallback signal only; SCB resolves ERP from learned structural evidence first, then site/system mapping, and uses the user impute only when stronger evidence is absent.

### Verification

- `py_compile src/brain/erp_native.py src/brain/erp_integration.py pages/6_Connectors.py tests/test_erp_integration.py` → **CLEAN**
- `pytest tests/test_erp_integration.py tests/test_erp_native.py tests/test_repository_catalog.py tests/test_pim_part_synthesis.py -q` → **14 passed**

## v0.22.55 - 2026-05-21

**Freight Entity Family Resolution — SCB vendor_consolidation LLM + OpenRouter/Grok Fallback**

### Added

- **`pipeline/docs/vendor_consolidation_package/generate_missing_shipper_consignee_ids_gard.py`** — new freight entity family resolution script. Generates shipper/consignee hash IDs for the March shipment file then runs a 5-layer DBA/parent-org clustering stack: (1) blocking key by name tokens + ZIP-5 + ISO-2 country, (2) multi-signal fuzzy composite score (40% name, 25% address, 20% ZIP, 15% city), (3) LLM verification via `dispatch_parallel("vendor_consolidation")`, (4) NetworkX connected-components graph clustering on confidence-weighted edges (≥ 0.80 threshold), (5) confidence-tier output (Certain/Probable/Inconclusive/Singleton). Outputs `Shipper_Families` and `Consignee_Families` sheets to `New_Shipper_Consignee_IDs.xlsx`.

- **`_vendor_consolidation_validator`** — structural scorer [0.0–1.0] passed as `validator=` to `dispatch_parallel`. Scores `is_same_family` bool (0.40), `confidence` in [0,1] (0.35), valid `relationship` enum (0.15), non-trivial `reasoning` (0.10). Fires `update_weights("vendor_consolidation", outcomes, score)` inline after every entity pair, closing the self-training loop without a separate training job.

- **`_call_openrouter_fallback()`** — direct OpenRouter/Grok-4.3 call via `llm_caller_openrouter.openrouter_caller` with a minimal `_GrokDecision` duck-type (`model_id="grok-4.3"`, `endpoint_env="XAI_API_KEY"`). Used automatically when the SCB ensemble is unavailable or raises. Inherits the existing xAI → OpenRouter backend fallover chain inside `_resolve_backend_chain`.

### Changed

- **`_call_scb_llm` routing** — SCB `dispatch_parallel("vendor_consolidation")` is now the **primary** path (99.5% of calls). On any exception it falls through to `_call_openrouter_fallback()` instead of returning an error dict. The `_SCB_AVAILABLE=False` early return is removed; fuzzy-only mode is now the last resort only when both SCB and OpenRouter/Grok are unreachable (`_SCB_AVAILABLE or _OPENROUTER_AVAILABLE` gate).

- **`pipeline/docs/vendor_consolidation_package/VENDOR_CONSOLIDATION_FINDINGS.md`** — added §1.4 "Freight Entity Family Resolution" (5-layer stack, output schema, asymmetric information strategy, confidence tier action table); updated §1.3 to document the inline validator and live training signal contribution; added `generate_missing_shipper_consignee_ids_gard.py` to the §6 files table.

- **`pipeline/docs/vendor_consolidation_package/VENDOR_CONSOLIDATION_HOWTO.md`** — added Part 6 (Steps 19–23): path setup, run command, interpreting family sheets, using `family_id` clusters as negotiation leverage, feeding confirmed/refuted clusters back to the SCB Mesh. Added `_gard.py` row to the Quick Reference table.

- **`pipeline/docs/vendor_consolidation_package/Refresh-VendorConsolidationTool.ps1`** — Step 2 now runs `generate_missing_shipper_consignee_ids_gard.py` automatically after the TCI workbook rebuild. Pass `-SkipFreightFamilies` to bypass (e.g., when Brain is offline).

### Verification

- `py_compile` on `generate_missing_shipper_consignee_ids_gard.py` → **CLEAN** (exit 0)
- Routing chain confirmed: SCB ensemble → OpenRouter/Grok-4.3 → error dict (no silent fuzzy-only degradation while any LLM path is live)

## v0.22.41 - 2026-05-20

**Graph-Native Mesh Learning Overlay**

### Added

- **`pipeline/src/brain/system_entirety.py`** - `oscillating_expansion_step()` now projects the recent learning window into bounded corpus topology. The overlay mints `MeshLearningWindow`, `LearningKindSummary`, `UEQGMRuntimeState`, and `PIMPlanningState` nodes, then spreads hardening through `EMITS_LEARNING_WINDOW`, `SUMMARIZES_LEARNING_KIND`, `HARDENS_ASSET_RESOURCE`, `HARDENS_MATERIAL_PROCESSOR`, and `HARDENS_ENDPOINT` edges.
- **`pipeline/docs/MESH_LEARNING_OVERLAY.md`** - documents the operator model, graph contract, persistence surfaces, and verification path for the distributed learning overlay.

### Changed

- **Mesh learning propagation** - System Entirety no longer treats learned state as scalar visibility only. Recent learning is summarized into graph-native windows and kind nodes, then distributed across asset resources, material processors, and endpoints for mesh hardening.
- **`pipeline/src/brain/asset_resource_mesh.py`** - asset mesh refreshes now preserve `mesh_learning`, `mesh_learning_projection`, and `mesh_learning_updated_at` while rewriting `entirety:physical_realization`, so learned hardening survives normal resource materialization cycles.

### Verification

- `python -m pytest pipeline/tests/test_system_entirety.py pipeline/tests/test_asset_resource_mesh.py -q` -> **29 passed**
- Live forced System Entirety expansion against `pipeline/local_brain.sqlite` minted learning overlay entities and hardening relations.

## v0.22.18 - 2026-05-19

**System Entirety Toroidal Analysis + Live Operational Pulse**

### Added

- **`docs/SYSTEM_ENTIRETY_ANALYSIS.md`** - added a 16-section System Entirety field manual that maps the Brain as a six-ring torus around the `heart` / `system_entirety` / `torus_touch` core. The document embeds a live `_system_entirety_report.py` snapshot, inventories all routed Streamlit pages and always-on daemons, traces the Oracle / Azure SQL / bridge / compute-grid surfaces, and provides a toroidal observability checklist plus explicit gap register.

### Changed

- **Live operational pulse coverage** - expanded the new analysis with release-time pulse metrics from `pipeline/local_brain.sqlite`: confirmed `llm_dispatch_log` has only **10 total rows**, all on **2026-05-04**; captured the current learning mix (`rag_deepdive`, `citation_chain`, `ocw_resource`, `ml_research`), `body_directives` load (**14 open**, **9 expired**), the current Entirety state (`bit_state=1`, `expansion_phase="broaden"`, `flip_count=3`), and the latest `systemic_refinement_log` cycle showing active compensation without successful self-train updates.
- **Root version metadata** - synced `VERSION` and `pipeline/src/brain/_version.py` to `0.22.18` for this documentation-and-verification release.

### Verification

- `cd pipeline && python3 -m py_compile _system_entirety_report.py src/brain/system_entirety.py src/brain/synaptic_workers.py bridge_rdp.py compute_node_daemon.py` → **passed**
- `cd pipeline && pytest -o "addopts=" tests/test_system_entirety.py tests/test_synaptic_workers.py tests/test_bridge_rdp.py tests/test_compute_grid_devtunnel.py tests/test_asset_resource_mesh.py tests/test_geospatial_relation.py -q --tb=short` → **53 passed** in **12.47s**
- `cd pipeline && python3 _system_entirety_report.py` → **passed**

## v0.22.12 - 2026-05-18

**Vendor Consolidation Package Refresh + Priority-Driven Workbook**

### Changed

- **`pipeline/src/brain/vendor_consolidation_excel.py`** - the vendor consolidation workbook no longer falls back to a flat 88% service level when TCI data omits a direct percentage. It now parses Excel-style dates robustly, infers service levels from service mode plus ship/delivery timing and supplier/service history, sorts tasks by priority, and writes a filterable `Priority_Tracker` sheet with explicit solution updates and workbook routes.
- **`pipeline/docs/vendor_consolidation_package/VENDOR_CONSOLIDATION_FINDINGS.md`**, **`pipeline/docs/vendor_consolidation_package/VENDOR_CONSOLIDATION_HOWTO.md`**, and **`pipeline/docs/vendor_consolidation_package/vendor_consolidation_data.csv`** - refreshed the handoff package so the docs and structured export match the current Summary, Operator Playbook, Priority Tracker, and What If workflow.

### Maintenance

- **Vendor package hygiene** - cleaned the local `pipeline/docs/vendor_consolidation_package/` folder by removing obsolete validation / `fixed` workbook variants, leaving the canonical handoff outputs in place. The `.xlsx` deliverables remain local package artifacts and are intentionally gitignored.

### Verification

- Generated both canonical package workbooks and verified the shipped output now shows **377 distinct service-level values** across the first 1,000 working rows instead of a flat 88%, with `Priority_Tracker` present and the current top metric showing **416 rows below the 95% service target**.

## v0.22.10 - 2026-05-18

**Credential Fallback Hardening + Works Cited Seed Resilience**

### Added

- **`pipeline/tests/test_llm_key_guard.py`**, **`pipeline/tests/test_llm_caller_openrouter.py`**, **`pipeline/tests/test_citation_chain_acquirer_seeding.py`**, and **`pipeline/tests/test_knowledge_corpus_scb_path.py`** - added focused regression coverage for local key rotation, xAI fallback transport, wrapped Works Cited seed payloads, and newest-export path resolution.

### Changed

- **`pipeline/src/brain/llm_key_guard.py`** and **`pipeline/src/brain/llm_caller_openrouter.py`** - the Brain now prefers project-local live credentials over stale inherited editor env vars, can clear cooldown when a key rotates locally, and can fall through from OpenRouter to direct xAI Grok transport when that is the only live path.
- **`pipeline/src/brain/doc_rag.py`**, **`pipeline/src/brain/knowledge_corpus.py`**, **`pipeline/src/brain/citation_chain_acquirer.py`**, and **`pipeline/reset_works_cited_cursor.py`** - document RAG and Works Cited ingestion now share the same live-key precedence, resolve the newest SCB export automatically, accept wrapped `paper_ids`/`seed_paper_ids` payloads, and reset stale Works Cited rows more completely before re-ingest.

### Verification

- Focused credential/citation regression: **33 passed** across `test_llm_key_guard.py`, `test_llm_caller_openrouter.py`, `test_doc_rag_credential.py`, `test_citation_chain_acquirer_seeding.py`, and `test_knowledge_corpus_scb_path.py`.

## v0.22.9 - 2026-05-18

**Adaptive UEQGM Runtime Daemon + System Entirety Consumption**

### Added

- **`pipeline/src/brain/synaptic_workers.py`** - added the dedicated `synapse-ueqgm` daemon, which refreshes the adaptive UEQGM runtime continuously from System Entirety certainty while recording heartbeat and runtime summary data into `brain_kv`.
- **`pipeline/tests/test_synaptic_workers.py`** - added focused coverage to ensure the new worker is started and reported by the synaptic health snapshot.

### Changed

- **`pipeline/src/brain/ueqgm_engine.py`** - adaptive runtime parameters are now evidence-gated per field; a candidate value only replaces the persisted state when newer evidence beats both the current corpus-density floor and the previously proven evidence for that parameter.
- **`pipeline/src/brain/system_entirety.py`** - the System Entirety now consumes the persisted UEQGM runtime as a symbiotic overlay, applying adaptive axis injections and a runtime-backed transaction contribution to spread proven UEQGM expansion through the broader system state.

### Verification

- Focused adaptive-runtime regression: **56 passed** across `test_ueqgm_engine.py`, `test_system_entirety.py`, and `test_synaptic_workers.py`.

## v0.22.8 - 2026-05-18

**Hideout Dev Tunnel Diagnostics + Resource-Share Planning**

### Added

- **`pipeline/bridge_rdp.py`** - added operator-facing Dev Tunnel flows for the hideout peer: local forward creation, mesh ping, hideout doctor, helper startup, and Dev Tunnel-backed RDP launch.
- **`pipeline/tests/test_bridge_rdp.py`** and **`pipeline/tests/test_compute_grid_devtunnel.py`** - added focused tests for `dt connect` port parsing, forward-state reuse, hideout health diagnostics, and remote mesh ping behavior.

### Changed

- **`pipeline/src/brain/compute_grid.py`** - local mesh execution now exposes `resource_inventory` and `resource_share_plan` payloads, so a Dev Tunnel peer can report shareable cores, RAM, VRAM, processor bindings, and contract coverage over the compute-grid protocol.
- **`pipeline/config/bridge_targets.yaml`** and **`pipeline/connect_hideout.ps1`** - wired `hideout-rdp` / `hideout-mesh` targets for `scbrain-hideout.use2`, and fixed the persistent helper so it no longer discards the requested `RemotePort` while writing forward-state metadata.

### Verification

- Focused Brain/bridge slice: **88 passed** across `test_system_entirety.py`, `test_asset_resource_mesh.py`, `test_geospatial_relation.py`, `test_compute_grid_devtunnel.py`, `test_bridge_rdp.py`, and `test_symbiotic_torus.py`.

## v0.21.0 - 2026-05-14

**Perception Mk2a - Heart-Grounded Audio/Video Ingestion**

### Added

- **`pipeline/src/brain/perception_audio.py`** - added Whisper STT for audio, bundled-ffmpeg audio extraction and keyframe sampling for full video, and a `perception_events` append-only log for heart-stamped percepts.
- **`pipeline/src/brain/perception.py`** - added `ingest_uploaded_asset()` so direct uploads route by media type: audio/video goes through Mk2a, while images and GIFs reuse the Mk1 visual analysis path and still return the same inline heart-stamped operator feedback.

### Changed

- **Heart-lock invariant** - `theta` now remains exactly locked to `heart.phase_rad`; any rADAM influence is recorded separately as `radam_phase_bias` metadata instead of perturbing the toroidal ground itself.
- **`pipeline/pages/17_Document_RAG.py`** - the sidebar uploader now supports the documented media set consistently and only warns about ffmpeg when a video upload actually needs it.
- **`pipeline/src/brain/brain_body_signals.py`** and **`pipeline/requirements.txt`** - added Touch-relief mappings and dependency wiring for transcript, keyframe, and audio-duration ingestion.

### Verification

- Added focused unit coverage for direct media upload routing and the exact Heart-lock stamp behavior.

## v0.20.22 - 2026-05-14

**System Entirety - 7th-D Observer-Tangent Bit Flip**

### Added

- **`pipeline/src/brain/system_entirety.py`** - introduced the 7th-dimensionality observer-tangent orchestrator. The Brain now projects the 6-sense CAT state onto a System Entirety axis, computes a Floquet-modulated bit flip, and persists `entirety:*` state to `brain_kv`.
- **`pipeline/tests/test_system_entirety.py`** - added unit coverage for observer geometry, parity flipping, period scaling, state shape, persistence, and rate-limit semantics.

### Changed

- **`pipeline/src/brain/knowledge_corpus.py`** - `refresh_corpus_round()` now invokes `oscillating_expansion_step()` and surfaces the returned entirety summary in the round output.
- **Root version metadata** - synced the repo `VERSION` file with the canonical `pipeline/src/brain/_version.py` value.

### Maintenance

- **Git hygiene** - Playwright failure screenshots under `pipeline/tests/playwright/screenshots/` are now ignored so local test artifacts do not dirty the repo.

## v0.20.19 - 2026-05-13

**Autonomous Resurrection Hardening**

### Fixed

- **`pipeline/autonomous_agent.py`** - process liveness now writes `pipeline/logs/agent_heartbeat.txt` every 60 seconds for the full life of the process, not only during long sleep windows. Busy cycles no longer appear dead to the resurrection layer.
- **`pipeline/brain_watchdog.ps1`** - watchdog startup now works on Windows PowerShell 5.1, quotes the agent path correctly under OneDrive paths with spaces, and captures agent stdout/stderr to dedicated log files for postmortem debugging.
- **`pipeline/install_brain_watchdog.ps1`** - scheduled task registration now launches through a wrapper command that survives Task Scheduler quoting edge cases and falls back to repeat-only registration when the logon trigger is blocked by local policy.

### Added

- **`pipeline/run_brain_watchdog.cmd`** - scheduler-safe wrapper that launches `brain_watchdog.ps1` hidden from `cmd.exe`.

### Operational Impact

- External watchdogs can now distinguish a busy healthy agent from a dead process.
- The process-level heartbeat remains compatible with the existing stale-heartbeat recovery logic in `pipeline/app.py`.

## v0.19.25 — 2026-05-01

**Repo Hygiene + Credential Migration**

### Credential Migration

- **`pipeline/config/connections.yaml`** — three new `rdp_*` scopes migrate all connection data from the deleted `pipeline/rdp_files/`:
  - `rdp_crp_fs03` — CRP File Server 03 (172.17.99.185:3389, Windows Integrated auth)
  - `rdp_desktop` — Desktop workstation (172.16.4.76:3389, Windows Integrated auth)
  - `rdp_laptop` — AGard Roadtec laptop (ROADL-4GVKFW3.roadtec.astec.local, `ASTECCORP\AGard`, NLA certificate thumbprint `218FA4BB0816EC0876241C5E987D1C6B2839D358`)
- Use `python -m src.connections.secrets set rdp_<name> --user <upn> --password <pw>` to store passwords for any RDP scope in the DPAPI vault.

### Cleanup

- Removed 22 one-off scripts: `quick_fix{1-5}.py`, 12 root-level `temp_*.py`, 7 `pipeline/temp_*.py` (debug, legend-fix, learning-report utilities)
- Finalised deletion of `Proxy-Pointer-RAG/` subproject (was deleted on disk, now removed from git index)
- Removed `pipeline/pages_archive/` (vestigial directory, contained only gitignored `__pycache__`)
- Removed `pipeline/rdp_files/` (3 `.rdp` files — connection info migrated to `connections.yaml`)

### Git / Artifact Hygiene

- `pipeline/abc_screenshots/` (442 PNG files, ~114 MB) — removed from git tracking; files retained on disk; pattern added to `.gitignore`
- `pipeline/navigation_tests/` (15 files, ~11 MB) — same treatment
- `pipeline/my_playwright_profile/` — added to `.gitignore` proactively
- Root `VERSION` file synced to canonical `_version.py` value (was stale at `0.19.4`)

### Version

- `pipeline/src/brain/_version.py` bumped `0.19.19 → 0.19.25`; PHASES table backfilled with `0.19.19` and `0.19.24` entries

---

## v0.19.4 — 2026-04-30

**One-Click Launcher (`Launch-SCB.ps1`)**

### New Files

- **`Launch-SCB.ps1`** — Root-level PowerShell launcher for the Supply Chain Architect. Double-click the desktop shortcut (or the script) to start the app with a single click:
  - Detects if SCB is already running on `localhost:8501`; if so, just opens the browser.
  - Otherwise spawns `streamlit run pipeline/app.py` as a fully hidden background process using the project venv.
  - Polls port 8501 (up to 30 s) before opening the default browser to `http://localhost:8501`.
  - Writes the Streamlit PID to `%TEMP%\scb_streamlit.pid` for optional cleanup.

### Desktop Shortcut

- **`Supply Chain Architect.lnk`** placed on the user Desktop (not committed — user-local artifact). The shortcut runs `powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File Launch-SCB.ps1`, presenting a zero-terminal, single-icon launch experience.

### Security

- No changes to `pipeline/.streamlit/config.toml` security controls (localhost-only binding, XSRF protection, CORS enforcement remain in force).

---

## v0.16.0 — 2026-04-24

**Symbiotic Dynamic Tunneling + Torus-Touch (T^7)**

### New Modules

- **`pipeline/src/brain/symbiotic_tunnel.py`** — Bayesian-Poisson centroids, inverted-ReLU ADAM optimiser, dual-floor mirror, and propeller routing. The vision worker mints `SYMBIOTIC_TUNNEL` edges from closed-loop TCP/UDP mesh topology every 5 minutes.

- **`pipeline/src/brain/torus_touch.py`** — Continuous 7-D categorical pressure on the toroidal manifold. Runs every 30 seconds pushing every Endpoint along the `n=7` categorical gap gradient so tunnel weights follow manifold geometry.

### New Scripts

- **`pipeline/fetch_pim_80446_v3.py`** — v3 PIM item fetch with screenshot capture for part 80446-04.
- **`pipeline/find_write_ops_80446.py`** — Discovery of write-operation touchpoints for item 80446-04 in Oracle Fusion PIM.

### Tests

- **`pipeline/tests/test_symbiotic_torus.py`** — 29 unit tests covering symbiotic tunnel centroid computation, torus-touch gradient steps, and edge-minting integration.

### Configuration

- Added root `.gitignore` to exclude `__pycache__`, `*.pyc`, `*.sqlite`, mission snapshots, and PIM screenshots from version control.

---

## v0.15.0 — 4-ERP xlsx pipeline (Epicor/Oracle/SyteLine/AX) + Brain page fixes + EOQ query optimisation.

## v0.14.9 — OCW semantic bridge + synaptic worker protection + network vision worker.

## v0.8.0 — Massive UX/Actionable overhaul: SQLite local store, NLP-part categorisation, Global date windows, semantic action-TODO engine.

## v0.7.1 — Ask the Data cross-dataset report generation.

## v0.7.0 — Enterprise Network Autonomous Agent. Native Exchange/SMB discovery.

## v0.6.0 — Global Application Filter, unified session_state routing, AI PowerPoint Creator.

## v0.5.0 — Value Stream Living Map: End-to-end integration of PO, SO, WO flows.

## v0.4.x — SQL query rewrites, database explorer, bug-fix waves.

## v0.3.0 — MIT CTL research modules: hierarchical EOQ, causal lead-time, survival, bullwhip, multi-echelon, sustainability, freight portfolio, CVaR risk.

## v0.2.0 — Depth: graph backends, LinUCB ranker, hierarchical OTD index.

## v0.1.0 — Core: EOQ Bayesian-Poisson, OTD recursive, Procurement 360, Data Quality, Connectors.
