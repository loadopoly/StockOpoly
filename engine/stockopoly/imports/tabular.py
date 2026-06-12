"""Stdlib table readers: CSV (csv module) and XLSX (zipfile + ElementTree).

``read_table(path)`` returns ``(headers, rows)`` with every cell as str |
float | bool | None. The XLSX path understands shared strings, inline
strings, booleans and numbers — the shapes ERP/WMS exports actually use.
Excel date *serials* stay numeric; importers convert fields they know are
dates via :func:`excel_serial_to_iso`.
"""
from __future__ import annotations

import csv
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")

Cell = str | float | bool | None


def read_csv(path: str | Path) -> tuple[list[str], list[list[Cell]]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = [row for row in reader if any(c.strip() for c in row)]
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    out: list[list[Cell]] = []
    for raw in rows[1:]:
        row: list[Cell] = []
        for cell in raw:
            cell = cell.strip()
            if cell == "":
                row.append(None)
                continue
            try:
                row.append(float(cell))
            except ValueError:
                row.append(cell)
        row += [None] * (len(headers) - len(row))
        out.append(row[:len(headers)])
    return headers, out


def _col_index(ref: str) -> int:
    m = _CELL_REF.match(ref or "")
    if not m:
        return 0
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root.findall(f"{_NS}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    return out


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
    if not names:
        raise ValueError("XLSX has no worksheets")
    return sorted(names, key=lambda n: int(re.search(r"(\d+)", n).group(1)))[0]


def read_xlsx(path: str | Path) -> tuple[list[str], list[list[Cell]]]:
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        root = ET.fromstring(zf.read(_first_sheet_path(zf)))
    matrix: list[list[Cell]] = []
    for row_el in root.iter(f"{_NS}row"):
        cells: dict[int, Cell] = {}
        for c in row_el.findall(f"{_NS}c"):
            idx = _col_index(c.get("r", ""))
            ctype = c.get("t", "n")
            v = c.find(f"{_NS}v")
            if ctype == "inlineStr":
                is_el = c.find(f"{_NS}is")
                cells[idx] = "".join(t.text or "" for t in is_el.iter(f"{_NS}t")) \
                    if is_el is not None else None
            elif v is None or v.text is None:
                cells[idx] = None
            elif ctype == "s":
                try:
                    cells[idx] = shared[int(v.text)]
                except (ValueError, IndexError):
                    cells[idx] = None
            elif ctype == "b":
                cells[idx] = v.text == "1"
            elif ctype == "str":
                cells[idx] = v.text
            else:
                try:
                    cells[idx] = float(v.text)
                except ValueError:
                    cells[idx] = v.text
        if cells:
            width = max(cells) + 1
            matrix.append([cells.get(i) for i in range(width)])
    if not matrix:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in matrix[0]]
    width = len(headers)
    body = []
    for r in matrix[1:]:
        if all(c is None or c == "" for c in r):
            continue
        r = (r + [None] * width)[:width]
        body.append(r)
    return headers, body


def read_table(path: str | Path) -> tuple[list[str], list[list[Cell]]]:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return read_csv(p)
    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx(p)
    raise ValueError(f"Unsupported table format: {suffix} (use .csv/.xlsx)")


def excel_serial_to_iso(value: Cell) -> str | None:
    """Excel date serial → 'YYYY-MM-DD'; ISO-ish strings pass through."""
    if value is None:
        return None
    if isinstance(value, float) and 20000 <= value <= 80000:
        d = datetime(1899, 12, 30) + timedelta(days=value)
        return d.date().isoformat()
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else s
