"""StockOpoly CLI — ``python -m stockopoly <command>``.

Commands::

    init                 create/upgrade the stockopoly.sqlite schema
    demo                 synthetic 96-bin warehouse end-to-end (no network)
    serve [--port 8181]  JSON API + built UI + /intake receiver
    ingest PATH          capture bundle (zip/dir) or folder of loose JPEGs
    solve                run the dimension solver over the current graph
    status               one-page JSON state summary
    sync                 push structured data + photos to Supabase (if enabled)
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from typing import Any


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stockopoly", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the database schema")
    sub.add_parser("demo", help="build the synthetic 96-bin demo warehouse")
    serve = sub.add_parser("serve", help="run the API/UI server")
    serve.add_argument("--port", type=int, default=8181)
    serve.add_argument("--host", default="127.0.0.1")
    ingest = sub.add_parser("ingest", help="ingest a bundle or photo folder")
    ingest.add_argument("path")
    ingest.add_argument("--force", action="store_true")
    sub.add_parser("solve", help="solve the dimension graph")
    sub.add_parser("status", help="print state summary")
    sync = sub.add_parser("sync", help="push structured data + photos to Supabase")
    sync.add_argument("--tables", nargs="*",
                      help="limit to these local tables (default: all + photos)")
    met = sub.add_parser("metrology", help="local standards + photo scales (holon.metrology/1)")
    msub = met.add_subparsers(dest="mcmd", required=True)
    mi = msub.add_parser("import-standards", help="load standards JSON from Metrology/metrology_cli.py")
    mi.add_argument("file")
    ma = msub.add_parser("apply-standard", help="ground a photo with a visible local standard")
    ma.add_argument("standard_key"); ma.add_argument("photo_id"); ma.add_argument("pixels", type=float)
    ma.add_argument("--pixel-sigma", type=float, default=2.0)
    ma.add_argument("--plane", default="default")
    ma.add_argument("--owner", required=True,
                    help="whose body/object is in the photo (must match a personal standard's owner)")
    mx = msub.add_parser("exif", help="log (and optionally solve with) a photo's EXIF prior")
    mx.add_argument("photo_id"); mx.add_argument("--force", action="store_true")
    mr = msub.add_parser("report", help="every scale source for a photo")
    mr.add_argument("photo_id")
    me = msub.add_parser("export-stats", help="unit-free error stats for Perceptopoly")
    me.add_argument("file")
    mg = msub.add_parser("measure-edge", help="measure edge sharpness in a box (needs Pillow)")
    mg.add_argument("photo_id"); mg.add_argument("box", help="x0,y0,x1,y1")
    mg.add_argument("--axis", choices=("vertical", "horizontal"), default="vertical")
    msub.add_parser("params", help="the dynamic calibration parameters in force")
    return p


def _cmd_init() -> dict[str, Any]:
    from .store import db_path, init_schema
    init_schema()
    return {"db": str(db_path()), "ok": True}


def _cmd_demo() -> dict[str, Any]:
    """96 bins (2 aisles × 6 bays × 4 levels × 2 bins), 24 parts, one year of
    usage, inventory, a solved reference dimension chain, velocity, occupancy,
    an optimized plan with migration tasks, and safety stock. Deterministic."""
    from . import dims, imports, locations, slotting
    from .partdims import normalize_all
    from .store import init_schema

    init_schema()
    rng = random.Random(42)

    codes = [f"{a}-{b:02d}-{lv}-{bn}" for a in "AB" for b in range(1, 7)
             for lv in range(1, 5) for bn in "AB"]
    locations.register_locations(codes, source="demo")

    parts = []
    for i in range(24):
        size = rng.choice([(6, 4, 3), (10, 8, 6), (14, 12, 10), (20, 16, 12)])
        parts.append({
            "part": f"DEMO-{i:03d}",
            "description": f"Demo part {i:03d}",
            "uom": "EA", "cost": round(rng.uniform(2, 120), 2),
            "length": size[0], "width": size[1], "height": size[2],
            "weight": round(rng.uniform(0.5, 12), 1),
        })
    imports.import_parts(parts)

    usage, inventory, po = [], [], []
    for i, part in enumerate(parts):
        pn = part["part"]
        base = max(1, int(rng.gauss(40, 30))) if i < 16 else rng.randint(0, 3)
        for m in range(1, 13):
            period = f"2026-{m:02d}" if m <= 6 else f"2025-{m:02d}"
            qty = max(0, int(rng.gauss(base, base * 0.3 + 1)))
            if qty:
                usage.append({"part": pn, "period": period, "qty": qty})
        spots = rng.sample(codes, rng.randint(1, 2))
        for code in spots:
            inventory.append({"part": pn, "location": code,
                              "qty": rng.randint(4, 60)})
        po.append({"po": f"PO-{1000 + i}", "part": pn, "qty": base * 2 or 5,
                   "unit price": part["cost"],
                   "order date": "2026-01-05",
                   "receipt date": f"2026-01-{5 + rng.randint(7, 25):02d}"})
    imports.import_usage(usage)
    imports.import_inventory(inventory)
    imports.import_po_history(po)

    # Photo-derived dimension chain: a tape-measure shot grounds bay width.
    ref = dims.add_reference_object("Tape measure (4 ft)", "ruler", dim_l=48.0,
                                    sigma_pct=0.5)
    dims.apply_reference(ref, photo_id="demo-photo-1", pixel_extents={"L": 480})
    bay = dims.create_entity("rack_member", "bay_width")
    bay_var = dims.ensure_variable(bay, "W")
    dims.add_measurement("pixel_extent", a_var=bay_var, value=1080,
                         photo_id="demo-photo-1", source="manual")
    solve_report = dims.solve_all()

    coords = locations.compute_coordinates()
    normalize_all()
    vel = slotting.compute_velocity()
    occ = slotting.compute_occupancy()
    plan = slotting.optimize({"demo": True})
    tasks = slotting.build_tasks(plan["plan_id"])
    ss = slotting.compute_safety_stock()

    return {
        "locations": len(codes), "parts": len(parts),
        "usage_rows": len(usage), "inventory_rows": len(inventory),
        "bay_width_solved_in": coords["bay_width_in"],
        "solver_rms": solve_report["rms"],
        "velocity_classes": vel["classes"],
        "occupancy": {k: occ[k] for k in ("over", "tight", "ok", "empty")},
        "plan": plan, "migration": tasks, "safety_stock": ss,
    }


def _cmd_ingest(path: str, force: bool) -> dict[str, Any]:
    from pathlib import Path

    from . import intake
    p = Path(path)
    if p.is_file() or (p / intake.MANIFEST_NAME).exists():
        return intake.ingest_bundle(p, force=force)
    return intake.ingest_loose(p)


def _cmd_status() -> dict[str, Any]:
    from . import scb_link
    from .store import db_path, open_conn
    cn = open_conn()
    try:
        counts = {t: cn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("batches", "photos", "photo_groups", "dim_entities",
                            "dim_measurements", "locations", "parts",
                            "inventory", "slotting_plans", "move_tasks")}
    finally:
        cn.close()
    return {"db": str(db_path()), "counts": counts, "scb": scb_link.status()}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        from .server import main as server_main
        return server_main(["--port", str(args.port), "--host", args.host])
    if args.command == "init":
        out = _cmd_init()
    elif args.command == "demo":
        out = _cmd_demo()
    elif args.command == "ingest":
        out = _cmd_ingest(args.path, args.force)
    elif args.command == "solve":
        from . import dims
        out = dims.solve_all()
    elif args.command == "sync":
        from . import supabase_sync
        out = supabase_sync.sync_now(args.tables or None)
    elif args.command == "metrology":
        from .store import init_schema
        from . import metrology
        init_schema()
        if args.mcmd == "import-standards":
            out = {"imported": metrology.import_standards(args.file)}
        elif args.mcmd == "apply-standard":
            out = metrology.apply_standard(args.standard_key, args.photo_id, args.pixels,
                                           pixel_sigma=args.pixel_sigma, plane=args.plane,
                                           photographed_owner=args.owner)
        elif args.mcmd == "exif":
            out = metrology.add_exif_prior(args.photo_id, force=args.force) or {
                "ok": False, "reason": "no focal_35mm/focus distance in EXIF"}
        elif args.mcmd == "report":
            out = metrology.photo_report(args.photo_id)
        elif args.mcmd == "measure-edge":
            out = metrology.measure_edge(args.photo_id,
                                         tuple(int(v) for v in args.box.split(",")), axis=args.axis)
        elif args.mcmd == "params":
            out = metrology.learned_params()
        else:
            out = {"stats": metrology.export_error_stats(args.file)}
    else:
        out = _cmd_status()
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
