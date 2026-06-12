"""CSV/XLSX readers + the four normalizing importers + ERP gating."""
from __future__ import annotations

import zipfile

from stockopoly import imports
from stockopoly.imports import erp
from stockopoly.imports.tabular import excel_serial_to_iso, read_table
from stockopoly.store import open_conn


def _q(sql, *args):
    cn = open_conn()
    try:
        return cn.execute(sql, args).fetchall()
    finally:
        cn.close()


def _write_xlsx(path, headers, rows):
    """Minimal real XLSX: shared strings for text, inline numbers."""
    shared: list[str] = []

    def cell(ref, value):
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        if value is None:
            return f'<c r="{ref}"/>'
        if value not in shared:
            shared.append(value)
        return f'<c r="{ref}" t="s"><v>{shared.index(value)}</v></c>'

    def col(i):
        out = ""
        i += 1
        while i:
            i, rem = divmod(i - 1, 26)
            out = chr(65 + rem) + out
        return out

    body = []
    for rno, row in enumerate([headers, *rows], start=1):
        cells = "".join(cell(f"{col(i)}{rno}", v) for i, v in enumerate(row))
        body.append(f'<row r="{rno}">{cells}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = (f'<?xml version="1.0"?><worksheet {ns}><sheetData>'
             + "".join(body) + "</sheetData></worksheet>")
    sst = (f'<?xml version="1.0"?><sst {ns} count="{len(shared)}"'
           f' uniqueCount="{len(shared)}">'
           + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("[Content_Types].xml", "<Types/>")
    return path


def test_read_csv_and_xlsx_agree(tmp_path):
    csv_p = tmp_path / "t.csv"
    csv_p.write_text("Part Number,Qty\nB-100,4\nB-200,2.5\n", encoding="utf-8")
    xlsx_p = _write_xlsx(tmp_path / "t.xlsx", ["Part Number", "Qty"],
                         [["B-100", 4], ["B-200", 2.5]])
    for p in (csv_p, xlsx_p):
        headers, rows = read_table(p)
        assert headers == ["Part Number", "Qty"]
        assert rows == [["B-100", 4.0], ["B-200", 2.5]]


def test_excel_serial_and_string_dates():
    assert excel_serial_to_iso(45992.0) == "2025-12-01"
    assert excel_serial_to_iso("2026-06-12") == "2026-06-12"
    assert excel_serial_to_iso("06/12/2026") == "2026-06-12"
    assert excel_serial_to_iso(None) is None


def test_import_parts_upsert(tmp_path):
    p = tmp_path / "parts.csv"
    p.write_text(
        "Item Number,Description,UOM,Unit Cost,Vendor,Length,Width,Height\n"
        "HYD-001,Hydraulic hose,EA,42.5,Gates,24,2,2\n"
        "HYD-002,Coupler,EA,7.25,Gates,,,\n"
        ",missing part number,EA,1,,,\n", encoding="utf-8")
    out = imports.import_parts(p)
    assert out["imported"] == 2 and out["skipped"] == 1
    row = _q("SELECT * FROM parts WHERE part_number='HYD-001'")[0]
    assert row["description"] == "Hydraulic hose"
    assert row["dim_l"] == 24.0 and row["dim_source"] == "import"

    # second import fills blanks without clobbering
    imports.import_parts([{"part": "HYD-002", "url": "https://g.example/c"}])
    row2 = _q("SELECT * FROM parts WHERE part_number='HYD-002'")[0]
    assert row2["supplier_url"] == "https://g.example/c"
    assert row2["description"] == "Coupler"


def test_import_inventory_registers_locations(tmp_path):
    p = tmp_path / "inv.csv"
    p.write_text("Part,Location,On Hand,UOM\n"
                 "HYD-001,A-01-1-A,12,EA\n"
                 "HYD-001,B-03-2,3,EA\n"
                 "HYD-009,??badloc,5,EA\n", encoding="utf-8")
    out = imports.import_inventory(p)
    assert out["imported"] == 3
    assert out["locations_added"] == 2 and out["locations_unparsed"] == 1
    inv = _q("SELECT * FROM inventory ORDER BY location_code")
    assert [r["location_code"] for r in inv] == ["??BADLOC", "A-01-1-A", "B-03-2"]
    # unknown parts auto-created
    assert _q("SELECT 1 FROM parts WHERE part_number='HYD-009'")


def test_import_po_history_with_serial_dates(tmp_path):
    rows = [
        {"PO Number": "PO-77", "Part": "HYD-001", "Qty": 10, "Unit Price": 41.0,
         "Order Date": 45992.0, "Receipt Date": "2026-01-05"},
        {"PO Number": "PO-77", "Part": "HYD-002", "Qty": 4, "Unit Price": 7.0,
         "Order Date": "2025-12-01"},
        {"Part": "NO-PO", "Qty": 1},
    ]
    out = imports.import_po_history(rows)
    assert out["imported"] == 2 and out["skipped"] == 1
    po = _q("SELECT * FROM po_history ORDER BY line_no")
    assert po[0]["order_date"] == "2025-12-01"
    assert po[0]["line_no"] == 1 and po[1]["line_no"] == 2  # auto line numbers


def test_import_usage_period_forms():
    out = imports.import_usage([
        {"Part": "HYD-001", "Period": "2026-04", "Qty": 7},
        {"Part": "HYD-001", "Month": 202605.0, "Qty": 9},
        {"Part": "HYD-001", "Date": "2026-06-03", "Qty": 2},
        {"Part": "HYD-001", "Qty": 5},                      # no period → skip
    ])
    assert out["imported"] == 3 and out["skipped"] == 1
    periods = [r["period"] for r in _q(
        "SELECT period FROM usage_history ORDER BY period")]
    assert periods == ["2026-04", "2026-05", "2026-06"]


def test_import_records_event():
    imports.import_usage([{"Part": "X", "Period": "2026-01", "Qty": 1}])
    kinds = [r["kind"] for r in _q("SELECT kind FROM events")]
    assert "stockopoly_import" in kinds


def test_erp_bridge_gated(monkeypatch):
    monkeypatch.delenv("STOCKOPOLY_ERP", raising=False)
    st = erp.erp_available()
    assert st["enabled"] is False and st["importable"] is False
    try:
        erp.pull("epicor", "inventory_on_hand")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "STOCKOPOLY_ERP" in str(exc)
    assert raised
