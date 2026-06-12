"""StockOpoly API + UI host — stdlib ThreadingHTTPServer, ``stockopoly.api/1``.

One process serves three things on port 8181:

* **JSON API** under ``/api/…`` (route table below) for the React app;
* **bundle intake** at ``/intake`` that is wire-compatible with the Brain's
  photogrammetry receiver (HEAD probe; multipart ``bundle``+``session``;
  raw ``application/zip`` body) — point the Operate Console's uplink URL at
  ``http://<host>:8181/intake`` and captures land here with zero app changes;
* the built React UI from ``app/dist`` (SPA fallback to index.html).

CORS is permissive: the dev UI runs on :3001 and the Operate Console on
:3000. Multipart parsing uses ``email.parser`` (no ``cgi`` in 3.13+).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import tempfile
import urllib.parse
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import API_VERSION, __version__, dims, events, grouping, intake, \
    scb_link, settings, slotting
from . import locations as loc_mod
from .imports import erp, import_inventory, import_parts, import_po_history, \
    import_usage
from .partdims import crawler, normalize_all
from .store import data_dir, db_path, open_conn

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # StockOpoly/
APP_DIST = _REPO_ROOT / "app" / "dist"
MAX_BODY_BYTES = 1 << 30

_IMPORTERS = {"parts": import_parts, "inventory": import_inventory,
              "po": import_po_history, "usage": import_usage}

_MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
         ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
         ".jpeg": "image/jpeg", ".json": "application/json",
         ".woff2": "font/woff2", ".ico": "image/x-icon",
         ".webmanifest": "application/manifest+json"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str, bytes]]:
    """→ [(field_name, filename, payload)] using the email package."""
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = BytesParser(policy=email_default_policy).parsebytes(header + body)
    out = []
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            out.append((name, part.get_filename() or "", payload))
    return out


# ───────────────────────────────────────────────────────────── API handlers
def _status(_req) -> dict:
    cn = open_conn()
    try:
        counts = {tbl: cn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                  for tbl in ("batches", "photos", "photo_groups", "locations",
                              "parts", "inventory", "move_tasks")}
    finally:
        cn.close()
    return {"api": API_VERSION, "version": __version__,
            "db": str(db_path()), "data_dir": str(data_dir()),
            "counts": counts, "scb": scb_link.status(),
            "erp": erp.erp_available(), "app_built": APP_DIST.exists()}


def _intake_bundle(req) -> dict:
    ctype = req.headers.get("Content-Type", "")
    zip_bytes: bytes | None = None
    if ctype.startswith("multipart/form-data"):
        for name, _fn, payload in _parse_multipart(ctype, req.body):
            if name == "bundle":
                zip_bytes = payload
    elif "zip" in ctype or req.path.rstrip("/").endswith("intake"):
        zip_bytes = req.body
    if not zip_bytes:
        raise ApiError(400, "No bundle payload (multipart field 'bundle' or raw zip)")
    tmp = Path(tempfile.mkdtemp(prefix="scb_uplink_")) / "bundle.zip"
    tmp.write_bytes(zip_bytes)
    try:
        summary = intake.ingest_bundle(tmp)
        return {"ok": True, **summary}
    except FileExistsError as exc:
        return {"ok": True, "duplicate": True, "detail": str(exc)}
    except (intake.BundleError, zipfile.BadZipFile) as exc:
        raise ApiError(422, f"Invalid bundle: {exc}") from exc
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def _intake_loose(req) -> dict:
    ctype = req.headers.get("Content-Type", "")
    if not ctype.startswith("multipart/form-data"):
        raise ApiError(400, "multipart/form-data with one or more 'photos' parts")
    tmp_dir = Path(tempfile.mkdtemp(prefix="loose_upload_"))
    try:
        saved = []
        for name, filename, payload in _parse_multipart(ctype, req.body):
            if name in ("photos", "files", "photo") and filename:
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)
                p = tmp_dir / (safe or f"photo_{len(saved)}.jpg")
                p.write_bytes(payload)
                saved.append(p)
        if not saved:
            raise ApiError(400, "No photo parts found")
        batch_name = req.query.get("name") or f"upload {len(saved)} files"
        return intake.ingest_loose(saved, name=batch_name)
    except intake.BundleError as exc:
        raise ApiError(422, str(exc)) from exc
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _photo_file(req) -> tuple[bytes, str]:
    batch_id, file = req.params["batch_id"], req.params["file"]
    cn = open_conn()
    try:
        row = cn.execute("SELECT abs_path FROM photos WHERE batch_id=? AND file=?",
                         (urllib.parse.unquote(batch_id),
                          urllib.parse.unquote(file))).fetchone()
    finally:
        cn.close()
    if row is None:
        raise ApiError(404, "Unknown photo")
    p = Path(row["abs_path"]).resolve()
    if not str(p).startswith(str(data_dir().resolve())) or not p.exists():
        raise ApiError(404, "Photo file missing")
    return p.read_bytes(), _MIME.get(p.suffix.lower(), "application/octet-stream")


def _import_table(req) -> dict:
    target = req.params["target"]
    fn = _IMPORTERS.get(target)
    if fn is None:
        raise ApiError(404, f"Unknown import target {target!r}"
                            f" (use {sorted(_IMPORTERS)})")
    ctype = req.headers.get("Content-Type", "")
    if ctype.startswith("multipart/form-data"):
        for name, filename, payload in _parse_multipart(ctype, req.body):
            if name in ("file", "table") and filename:
                suffix = Path(filename).suffix.lower() or ".csv"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
                    fh.write(payload)
                    tmp = Path(fh.name)
                try:
                    return fn(tmp)
                finally:
                    tmp.unlink(missing_ok=True)
        raise ApiError(400, "multipart field 'file' required")
    rows = req.json.get("rows")
    if not isinstance(rows, list):
        raise ApiError(400, "JSON body {'rows': [...]} or multipart file required")
    return fn(rows)


def _erp_pull(req) -> dict:
    body = req.json
    target = body.get("target")
    if target not in _IMPORTERS:
        raise ApiError(400, f"target must be one of {sorted(_IMPORTERS)}")
    try:
        rows = erp.pull(body.get("connector") or "", body.get("logical") or "")
    except RuntimeError as exc:
        raise ApiError(503, str(exc)) from exc
    return {"pulled": len(rows), **_IMPORTERS[target](rows)}


def _map_data(req) -> dict:
    plan_id = req.query.get("plan_id")
    cn = open_conn()
    try:
        locs = [dict(r) for r in cn.execute(
            "SELECT l.*, o.occupancy_pct, o.status AS occ_status, o.part_count"
            " FROM locations l LEFT JOIN occupancy o"
            " ON o.location_code=l.location_code WHERE l.x IS NOT NULL")]
        layout = [dict(r) for r in cn.execute("SELECT * FROM layout")]
        future = {}
        if plan_id:
            for r in cn.execute(
                    "SELECT location_code, COUNT(*) AS parts, SUM(qty_target) AS qty"
                    " FROM assignments_future WHERE plan_id=? GROUP BY location_code",
                    (plan_id,)):
                future[r["location_code"]] = {"parts": r["parts"], "qty": r["qty"]}
    finally:
        cn.close()
    return {"locations": locs, "layout": layout, "future": future}


def _parts_list(req) -> dict:
    limit = int(req.query.get("limit", 500))
    q = (req.query.get("q") or "").strip()
    cn = open_conn()
    try:
        sql = ("SELECT p.*, v.abc_class, v.xyz_class, v.velocity_score,"
               " v.months_of_supply, s.ss_qty, s.min_qty, s.max_qty"
               " FROM parts p LEFT JOIN velocity v ON v.part_number=p.part_number"
               " LEFT JOIN ss_params s ON s.part_number=p.part_number")
        args: list = []
        if q:
            sql += " WHERE p.part_number LIKE ? OR p.description LIKE ?"
            args += [f"%{q}%", f"%{q}%"]
        sql += " ORDER BY v.velocity_score DESC NULLS LAST, p.part_number LIMIT ?"
        args.append(limit)
        return {"parts": [dict(r) for r in cn.execute(sql, args)]}
    finally:
        cn.close()


# ────────────────────────────────────────────────────────────── route table
def _routes() -> list[tuple[str, re.Pattern, Callable]]:
    J = lambda fn: fn  # noqa: E731 - readability tag for JSON handlers
    table: list[tuple[str, str, Callable]] = [
        ("GET", r"/api/status", J(_status)),
        # intake
        ("POST", r"/intake", _intake_bundle),
        ("POST", r"/", _intake_bundle),
        ("POST", r"/api/intake/loose", _intake_loose),
        ("GET", r"/api/batches", lambda r: {"batches": intake.list_batches()}),
        ("GET", r"/api/batches/(?P<batch_id>[^/]+)/photos",
         lambda r: {"photos": intake.batch_photos(
             urllib.parse.unquote(r.params["batch_id"]))}),
        ("GET", r"/api/photos/(?P<batch_id>[^/]+)/(?P<file>.+)", _photo_file),
        # grouping
        ("POST", r"/api/groups/cascade",
         lambda r: grouping.run_cascade(r.json["batch_id"])),
        ("GET", r"/api/groups",
         lambda r: {"groups": grouping.list_groups(r.query.get("batch_id", ""))}),
        ("POST", r"/api/groups",
         lambda r: {"group_id": grouping.create_group(
             r.json["batch_id"], r.json.get("kind", "same_object"),
             r.json.get("label", "Group"), r.json.get("photo_ids", []))}),
        ("POST", r"/api/groups/(?P<gid>[^/]+)/confirm",
         lambda r: grouping.confirm_group(r.params["gid"],
                                          bool(r.json.get("confirmed", True))) or {"ok": True}),
        ("PUT", r"/api/groups/(?P<gid>[^/]+)/members",
         lambda r: grouping.set_members(r.params["gid"],
                                        r.json.get("photo_ids", [])) or {"ok": True}),
        ("DELETE", r"/api/groups/(?P<gid>[^/]+)",
         lambda r: grouping.delete_group(r.params["gid"]) or {"ok": True}),
        # dims
        ("GET", r"/api/dims/entities", lambda r: {"entities": dims.list_entities()}),
        ("POST", r"/api/dims/entities",
         lambda r: {"entity_id": dims.create_entity(
             r.json.get("kind", "object"), r.json.get("label", "Entity"),
             group_id=r.json.get("group_id"), photo_id=r.json.get("photo_id"),
             notes=r.json.get("notes", ""))}),
        ("POST", r"/api/dims/variables",
         lambda r: {"var_id": dims.ensure_variable(int(r.json["entity_id"]),
                                                   r.json["axis"])}),
        ("POST", r"/api/dims/measurements",
         lambda r: {"meas_id": dims.add_measurement(
             r.json["kind"], a_var=int(r.json["a_var"]),
             value=float(r.json["value"]),
             b_var=int(r.json["b_var"]) if r.json.get("b_var") else None,
             photo_id=r.json.get("photo_id"),
             part_vars=[int(v) for v in r.json.get("part_vars", [])] or None,
             sigma=float(r.json["sigma"]) if r.json.get("sigma") else None,
             unit=r.json.get("unit", "in"), source=r.json.get("source", "manual"),
             meta=r.json.get("meta"))}),
        ("DELETE", r"/api/dims/measurements/(?P<mid>\d+)",
         lambda r: dims.delete_measurement(int(r.params["mid"])) or {"ok": True}),
        ("GET", r"/api/dims/measurements",
         lambda r: {"measurements": dims.list_measurements()}),
        ("POST", r"/api/dims/solve", lambda r: dims.solve_all()),
        ("GET", r"/api/dims/references",
         lambda r: {"references": dims.list_reference_objects()}),
        ("POST", r"/api/dims/references",
         lambda r: {"ref_id": dims.add_reference_object(
             r.json["name"], r.json.get("kind", "ruler"),
             dim_l=r.json.get("dim_l"), dim_w=r.json.get("dim_w"),
             dim_h=r.json.get("dim_h"), unit=r.json.get("unit", "in"),
             sigma_pct=float(r.json.get("sigma_pct", 1.0)))}),
        ("POST", r"/api/dims/references/(?P<rid>\d+)/apply",
         lambda r: dims.apply_reference(int(r.params["rid"]),
                                        photo_id=r.json["photo_id"],
                                        pixel_extents=r.json.get("pixel_extents", {}))),
        # locations
        ("GET", r"/api/locations",
         lambda r: {"locations": loc_mod.list_locations(r.query.get("zone"))}),
        ("POST", r"/api/locations/register",
         lambda r: loc_mod.register_locations(
             r.json.get("codes", []), zone=r.json.get("zone"),
             source=r.json.get("source", "manual"))),
        ("POST", r"/api/locations/coordinates",
         lambda r: loc_mod.compute_coordinates()),
        ("GET", r"/api/locations/travel", lambda r: {"costs": loc_mod.travel_costs()}),
        ("POST", r"/api/locations/dock",
         lambda r: loc_mod.set_dock(float(r.json["x"]), float(r.json["y"]))
         or {"ok": True}),
        ("GET", r"/api/map", _map_data),
        # imports + parts
        ("POST", r"/api/import/(?P<target>[a-z]+)", _import_table),
        ("GET", r"/api/erp/status", lambda r: erp.erp_available()),
        ("POST", r"/api/erp/pull", _erp_pull),
        ("GET", r"/api/parts", _parts_list),
        ("POST", r"/api/partdims/normalize", lambda r: normalize_all()),
        # crawler
        ("GET", r"/api/crawl/results",
         lambda r: {"results": crawler.list_results(
             pending_only=r.query.get("all") != "1")}),
        ("POST", r"/api/crawl/part",
         lambda r: crawler.crawl_part(r.json["part_number"])),
        ("POST", r"/api/crawl/results/(?P<rid>\d+)/accept",
         lambda r: crawler.accept_result(int(r.params["rid"]))),
        ("POST", r"/api/crawl/results/(?P<rid>\d+)/reject",
         lambda r: crawler.reject_result(int(r.params["rid"])) or {"ok": True}),
        # slotting
        ("POST", r"/api/slotting/velocity", lambda r: slotting.compute_velocity()),
        ("GET", r"/api/slotting/velocity",
         lambda r: {"velocity": slotting.list_velocity()}),
        ("POST", r"/api/slotting/occupancy", lambda r: slotting.compute_occupancy()),
        ("GET", r"/api/slotting/current",
         lambda r: {"locations": slotting.current_state(r.query.get("status"))}),
        ("POST", r"/api/slotting/optimize",
         lambda r: slotting.optimize(r.json or {})),
        ("GET", r"/api/slotting/plans", lambda r: {"plans": slotting.list_plans()}),
        ("GET", r"/api/slotting/plans/(?P<pid>[^/]+)/assignments",
         lambda r: {"assignments": slotting.plan_assignments(r.params["pid"])}),
        ("POST", r"/api/slotting/plans/(?P<pid>[^/]+)/tasks",
         lambda r: slotting.build_tasks(r.params["pid"])),
        ("GET", r"/api/slotting/plans/(?P<pid>[^/]+)/tasks",
         lambda r: {"tasks": slotting.list_tasks(
             r.params["pid"],
             int(r.query["day"]) if r.query.get("day") else None)}),
        ("POST", r"/api/slotting/tasks/(?P<tid>\d+)/complete",
         lambda r: slotting.complete_task(int(r.params["tid"]))),
        ("POST", r"/api/slotting/safety-stock",
         lambda r: slotting.compute_safety_stock(r.json.get("scenario"))),
        ("GET", r"/api/slotting/safety-stock", lambda r: {"ss": slotting.list_ss()}),
        ("GET", r"/api/slotting/safety-stock/(?P<part>[^/]+)/compare",
         lambda r: slotting.scenario_compare(urllib.parse.unquote(r.params["part"]))),
        # settings / events / scb
        ("GET", r"/api/settings", lambda r: settings.all_settings()),
        ("PUT", r"/api/settings", lambda r: settings.update(r.json or {})),
        ("GET", r"/api/events",
         lambda r: {"events": events.recent(int(r.query.get("limit", 50)))}),
        ("GET", r"/api/scb/status", lambda r: scb_link.status()),
        ("POST", r"/api/scb/flush",
         lambda r: {"flushed": scb_link.flush_outbox()}),
    ]
    return [(m, re.compile("^" + p + "$"), fn) for m, p, fn in table]


ROUTES = _routes()


class Request:
    """What handlers see: headers, parsed query/json/body, path params."""

    def __init__(self, handler: "Handler", params: dict[str, str]):
        self.headers = handler.headers
        self.path = handler.path
        self.params = params
        parsed = urllib.parse.urlparse(handler.path)
        self.query = {k: v[0] for k, v in
                      urllib.parse.parse_qs(parsed.query).items()}
        self.body = handler.body
        self._json: dict | None = None

    @property
    def json(self) -> dict:
        if self._json is None:
            try:
                self._json = json.loads(self.body.decode("utf-8")) if self.body else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(400, f"Invalid JSON body: {exc}") from exc
            if not isinstance(self._json, dict):
                raise ApiError(400, "JSON body must be an object")
        return self._json


class Handler(BaseHTTPRequestHandler):
    server_version = f"StockOpoly/{__version__}"
    body: bytes = b""

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods",
                         "GET, POST, PUT, DELETE, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-File-Name")

    def _send(self, status: int, payload: bytes, ctype: str) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> bool:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "Body too large"})
            return False
        self.body = self.rfile.read(length) if length else b""
        return True

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlparse(self.path).path
        for m, pattern, fn in ROUTES:
            if m != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            if not self._read_body():
                return
            try:
                result = fn(Request(self, match.groupdict()))
            except ApiError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            except (KeyError, ValueError) as exc:
                self._send_json(400, {"error": f"{type(exc).__name__}: {exc}"})
                return
            except Exception as exc:  # pragma: no cover - safety net
                logger.exception("Handler error on %s %s", method, path)
                self._send_json(500, {"error": f"Internal error: {exc}"})
                return
            if isinstance(result, tuple):  # (bytes, mime)
                self._send(200, result[0], result[1])
            else:
                self._send_json(200, result if result is not None else {"ok": True})
            return
        if method in ("GET", "HEAD"):
            self._static(path)
        else:
            self._send_json(404, {"error": f"No route {method} {path}"})

    def _static(self, path: str) -> None:
        if path.startswith("/api/"):
            self._send_json(404, {"error": f"No route GET {path}"})
            return
        if not APP_DIST.exists():
            self._send_json(200, {
                "service": "StockOpoly", "api": API_VERSION,
                "hint": "UI not built — run `npm run build` in app/,"
                        " or use the dev server on :3001"})
            return
        rel = path.lstrip("/") or "index.html"
        target = (APP_DIST / rel).resolve()
        if not str(target).startswith(str(APP_DIST.resolve())) or not target.is_file():
            target = APP_DIST / "index.html"  # SPA fallback
        self._send(200, target.read_bytes(),
                   _MIME.get(target.suffix.lower(), "application/octet-stream"))

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_HEAD(self) -> None:  # uplink reachability probe
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()


def serve(port: int = 8181, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    from .store import init_schema
    init_schema()
    httpd = ThreadingHTTPServer((host, port), Handler)
    logger.info("StockOpoly engine on http://%s:%d (api %s)", host, port, API_VERSION)
    return httpd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stockopoly.server")
    parser.add_argument("--port", type=int, default=8181)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    httpd = serve(args.port, args.host)
    stamp = datetime.now(timezone.utc).isoformat()
    print(f"[{stamp}] StockOpoly listening on http://{args.host}:{args.port}"
          f" — intake at /intake, API at /api/status")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
