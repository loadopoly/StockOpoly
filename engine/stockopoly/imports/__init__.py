"""Normalizing importers: parts / inventory / PO history / usage.

Each importer accepts a CSV/XLSX path **or** pre-fetched ``list[dict]`` rows
(the ERP bridge feeds dicts), matches headers against a synonym table, and
upserts into the store. Unknown columns are ignored; rows missing required
fields are counted as skipped, never fatal. Inventory imports auto-register
their location codes so the map stays in sync, and auto-create unknown
parts so joins never dangle.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import events, locations
from ..store import open_conn
from .tabular import excel_serial_to_iso, read_table

__all__ = ["import_parts", "import_inventory", "import_po_history",
           "import_usage", "rows_from_table"]


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


_SYNONYMS: dict[str, dict[str, str]] = {
    "parts": {
        "partnumber": "part_number", "part": "part_number", "item": "part_number",
        "itemnumber": "part_number", "sku": "part_number", "partno": "part_number",
        "description": "description", "desc": "description",
        "itemdescription": "description",
        "uom": "uom", "unit": "uom", "unitofmeasure": "uom",
        "unitcost": "unit_cost", "cost": "unit_cost", "stdcost": "unit_cost",
        "price": "unit_cost",
        "suppliername": "supplier_name", "supplier": "supplier_name",
        "vendor": "supplier_name", "vendorname": "supplier_name",
        "supplierpartnumber": "supplier_part_number",
        "vendorpartnumber": "supplier_part_number", "mfgpart": "supplier_part_number",
        "supplierurl": "supplier_url", "url": "supplier_url", "link": "supplier_url",
        "length": "dim_l", "width": "dim_w", "height": "dim_h", "weight": "weight",
    },
    "inventory": {
        "partnumber": "part_number", "part": "part_number", "item": "part_number",
        "sku": "part_number", "partno": "part_number",
        "locationcode": "location_code", "location": "location_code",
        "bin": "location_code", "binlocation": "location_code",
        "qtyoh": "qty_oh", "qty": "qty_oh", "onhand": "qty_oh",
        "qtyonhand": "qty_oh", "quantity": "qty_oh",
        "uom": "uom", "unit": "uom",
        "asof": "as_of", "date": "as_of", "snapshotdate": "as_of",
    },
    "po": {
        "poid": "po_id", "ponumber": "po_id", "po": "po_id",
        "purchaseorder": "po_id",
        "lineno": "line_no", "line": "line_no", "poline": "line_no",
        "partnumber": "part_number", "part": "part_number", "item": "part_number",
        "sku": "part_number",
        "suppliername": "supplier_name", "supplier": "supplier_name",
        "vendor": "supplier_name",
        "qty": "qty", "qtyordered": "qty", "quantity": "qty",
        "unitprice": "unit_price", "price": "unit_price", "unitcost": "unit_price",
        "orderdate": "order_date", "podate": "order_date", "created": "order_date",
        "receiptdate": "receipt_date", "received": "receipt_date",
        "receipt": "receipt_date",
    },
    "usage": {
        "partnumber": "part_number", "part": "part_number", "item": "part_number",
        "sku": "part_number",
        "period": "period", "month": "period", "yyyymm": "period",
        "qty": "qty", "quantity": "qty", "issued": "qty", "used": "qty",
        "kind": "kind", "type": "kind",
        "date": "date", "transactiondate": "date", "issuedate": "date",
    },
}


def rows_from_table(source: str | Path | Iterable[dict], kind: str) -> list[dict]:
    """Normalize a file or dict-rows into canonical-keyed dicts."""
    syn = _SYNONYMS[kind]
    if isinstance(source, (str, Path)):
        headers, body = read_table(source)
        mapped = [syn.get(_norm(h)) for h in headers]
        out = []
        for raw in body:
            row = {}
            for key, cell in zip(mapped, raw):
                if key and cell is not None and cell != "":
                    row[key] = cell
            if row:
                out.append(row)
        return out
    out = []
    for d in source:
        row = {}
        for k, v in dict(d).items():
            key = syn.get(_norm(k))
            if key and v is not None and v != "":
                row[key] = v
        if row:
            out.append(row)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip() or None


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _record(kind_label: str, imported: int, skipped: int, extra: dict | None = None):
    payload = {"target": kind_label, "imported": imported, "skipped": skipped,
               **(extra or {})}
    events.record("stockopoly_import", payload,
                  title=f"Import {kind_label}: {imported} row(s)",
                  signal=min(0.3 + imported / 1000.0, 0.8))
    return payload


def _ensure_part(cn, part_number: str) -> None:
    cn.execute("INSERT OR IGNORE INTO parts(part_number, updated_at) VALUES (?,?)",
               (part_number, _now()))


# ──────────────────────────────────────────────────────────────────── loaders
def import_parts(source: str | Path | Iterable[dict]) -> dict[str, Any]:
    rows = rows_from_table(source, "parts")
    imported = skipped = 0
    cn = open_conn()
    try:
        for r in rows:
            pn = _s(r.get("part_number"))
            if not pn:
                skipped += 1
                continue
            cn.execute(
                "INSERT INTO parts(part_number, description, uom, unit_cost,"
                " supplier_name, supplier_part_number, supplier_url,"
                " dim_l, dim_w, dim_h, weight, dim_source, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(part_number) DO UPDATE SET"
                " description=COALESCE(excluded.description, parts.description),"
                " uom=COALESCE(excluded.uom, parts.uom),"
                " unit_cost=COALESCE(excluded.unit_cost, parts.unit_cost),"
                " supplier_name=COALESCE(excluded.supplier_name, parts.supplier_name),"
                " supplier_part_number=COALESCE(excluded.supplier_part_number,"
                "   parts.supplier_part_number),"
                " supplier_url=COALESCE(excluded.supplier_url, parts.supplier_url),"
                " dim_l=COALESCE(excluded.dim_l, parts.dim_l),"
                " dim_w=COALESCE(excluded.dim_w, parts.dim_w),"
                " dim_h=COALESCE(excluded.dim_h, parts.dim_h),"
                " weight=COALESCE(excluded.weight, parts.weight),"
                " updated_at=excluded.updated_at",
                (pn, _s(r.get("description")), _s(r.get("uom")),
                 _f(r.get("unit_cost")), _s(r.get("supplier_name")),
                 _s(r.get("supplier_part_number")), _s(r.get("supplier_url")),
                 _f(r.get("dim_l")), _f(r.get("dim_w")), _f(r.get("dim_h")),
                 _f(r.get("weight")),
                 "import" if any(r.get(k) for k in ("dim_l", "dim_w", "dim_h"))
                 else None,
                 _now()))
            imported += 1
        cn.commit()
    finally:
        cn.close()
    return _record("parts", imported, skipped)


def import_inventory(source: str | Path | Iterable[dict]) -> dict[str, Any]:
    rows = rows_from_table(source, "inventory")
    imported = skipped = 0
    codes: set[str] = set()
    today = datetime.now(timezone.utc).date().isoformat()
    cn = open_conn()
    try:
        for r in rows:
            pn, loc = _s(r.get("part_number")), _s(r.get("location_code"))
            qty = _f(r.get("qty_oh"))
            if not pn or not loc or qty is None:
                skipped += 1
                continue
            loc = loc.upper()
            codes.add(loc)
            _ensure_part(cn, pn)
            cn.execute(
                "INSERT INTO inventory(part_number, location_code, qty_oh, uom,"
                " as_of) VALUES (?,?,?,?,?)"
                " ON CONFLICT(part_number, location_code) DO UPDATE SET"
                " qty_oh=excluded.qty_oh, uom=COALESCE(excluded.uom, inventory.uom),"
                " as_of=excluded.as_of",
                (pn, loc, qty, _s(r.get("uom")),
                 excel_serial_to_iso(r.get("as_of")) or today))
            imported += 1
        cn.commit()
    finally:
        cn.close()
    reg = locations.register_locations(sorted(codes), source="import") if codes else \
        {"added": 0, "failed": []}
    return _record("inventory", imported, skipped,
                   {"locations_added": reg["added"],
                    "locations_unparsed": len(reg["failed"])})


def import_po_history(source: str | Path | Iterable[dict]) -> dict[str, Any]:
    rows = rows_from_table(source, "po")
    imported = skipped = 0
    cn = open_conn()
    try:
        line_seq: dict[str, int] = {}
        for r in rows:
            po, pn = _s(r.get("po_id")), _s(r.get("part_number"))
            if not po or not pn:
                skipped += 1
                continue
            line = r.get("line_no")
            if line is None:
                line_seq[po] = line_seq.get(po, 0) + 1
                line = line_seq[po]
            _ensure_part(cn, pn)
            cn.execute(
                "INSERT OR REPLACE INTO po_history(po_id, line_no, part_number,"
                " supplier_name, qty, unit_price, order_date, receipt_date)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (po, int(_f(line) or 0), pn, _s(r.get("supplier_name")),
                 _f(r.get("qty")), _f(r.get("unit_price")),
                 excel_serial_to_iso(r.get("order_date")),
                 excel_serial_to_iso(r.get("receipt_date"))))
            imported += 1
        cn.commit()
    finally:
        cn.close()
    return _record("po_history", imported, skipped)


def import_usage(source: str | Path | Iterable[dict]) -> dict[str, Any]:
    rows = rows_from_table(source, "usage")
    imported = skipped = 0
    cn = open_conn()
    try:
        for r in rows:
            pn, qty = _s(r.get("part_number")), _f(r.get("qty"))
            period = _s(r.get("period"))
            if not period and r.get("date") is not None:
                iso = excel_serial_to_iso(r.get("date"))
                period = iso[:7] if iso and len(iso) >= 7 else None
            if isinstance(r.get("period"), float):  # e.g. 202605
                raw = str(int(r["period"]))
                if len(raw) == 6:
                    period = f"{raw[:4]}-{raw[4:]}"
            if not pn or qty is None or not period or not re.match(r"^\d{4}-\d{2}$", period):
                skipped += 1
                continue
            _ensure_part(cn, pn)
            cn.execute(
                "INSERT INTO usage_history(part_number, period, qty, kind)"
                " VALUES (?,?,?,?) ON CONFLICT(part_number, period, kind)"
                " DO UPDATE SET qty=excluded.qty",
                (pn, period, qty, _s(r.get("kind")) or "issue"))
            imported += 1
        cn.commit()
    finally:
        cn.close()
    return _record("usage", imported, skipped)
