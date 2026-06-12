"""Container normalization + crawler extraction/politeness/accept workflow."""
from __future__ import annotations

import pytest

from stockopoly import imports, partdims
from stockopoly.partdims import crawler
from stockopoly.store import open_conn


def _q(sql, *args):
    cn = open_conn()
    try:
        return cn.execute(sql, args).fetchall()
    finally:
        cn.close()


# ──────────────────────────────────────────────────────────── normalization
def test_fit_qty_orientations():
    assert partdims.fit_qty((12, 10, 8), (12, 10, 8)) == 1
    assert partdims.fit_qty((6, 5, 4), (12, 10, 8)) == 8
    # rotation required: 10×2×2 part in a 12×10×8 box → lay it down
    assert partdims.fit_qty((10, 2, 2), (12, 10, 8)) >= 20
    assert partdims.fit_qty((20, 20, 20), (12, 10, 8)) == 0


def test_normalize_picks_smallest_box_and_weight_caps():
    imports.import_parts([
        {"part": "SMALL", "length": 6, "width": 5, "height": 4},
        {"part": "HEAVY", "length": 6, "width": 5, "height": 4, "weight": 20},
        {"part": "NODIMS"},
        {"part": "HUGE", "length": 47, "width": 39, "height": 35},
    ])
    out = partdims.normalize_all()
    assert out["normalized"] == 3 and out["missing_dims"] == 1

    rows = {r["part_number"]: r for r in _q("SELECT * FROM parts")}
    assert rows["SMALL"]["norm_box_id"] == "carton_s"
    assert rows["SMALL"]["norm_qty_per_box"] == 8
    # 8 would exceed carton_s 40 lb at 20 lb/unit → capped at 2
    assert rows["HEAVY"]["norm_qty_per_box"] == 2
    assert rows["HUGE"]["norm_box_id"] == "gaylord"
    assert rows["NODIMS"]["norm_box_id"] is None


# ─────────────────────────────────────────────────────────── dim extraction
def test_extract_dims_patterns():
    page = ('Product specs — Dimensions: 24" x 18.5" x 12" — ships fast. '
            "Also available: 300 x 200 x 150 mm metric version.")
    cands = crawler.extract_dims(page)
    assert cands[0]["dim_l"] == 24.0 and cands[0]["dim_w"] == 18.5
    assert cands[0]["confidence"] == 0.8
    metric = [c for c in cands if c["dim_l"] == pytest.approx(11.811, abs=0.01)]
    assert metric, "mm triple should convert to inches"
    assert crawler.extract_dims("nothing here 5 stars x 4 reviews") == []


def test_extract_weight():
    assert crawler.extract_weight("Weight: 3.5 lbs each") == 3.5
    assert crawler.extract_weight("Mass 2 kg") == pytest.approx(4.409, abs=0.01)
    assert crawler.extract_weight("no weight") is None


# ───────────────────────────────────────────── crawl workflow (no network)
@pytest.fixture()
def fake_site(monkeypatch):
    pages = {
        "https://supplier.example/p/HYD-001":
            'Hose HYD-001. Dimensions: 24 in x 2 in x 2 in. Weight: 3 lbs.',
    }
    calls = {"fetch": 0}

    def fake_fetch(url, timeout=20.0):
        calls["fetch"] += 1
        return (200, f"<html><body>{pages.get(url, 'not found')}</body></html>") \
            if url in pages else (404, "")

    monkeypatch.setattr(crawler, "_fetch", fake_fetch)
    monkeypatch.setattr(crawler, "_robots_allowed", lambda url, cn: True)
    crawler._last_fetch.clear()
    return calls


def test_crawl_accept_workflow(fake_site):
    imports.import_parts([{"part": "HYD-001",
                           "url": "https://supplier.example/p/HYD-001"}])
    out = crawler.crawl_part("HYD-001", min_interval_s=0)
    assert out["results"] == 1
    pending = crawler.list_results()
    assert len(pending) == 1
    cand = pending[0]
    assert cand["dim_l"] == 24.0 and cand["weight"] == 3.0
    assert "Dimensions" in cand["provenance_excerpt"]

    # dims do NOT land on the part until a human accepts
    part = _q("SELECT dim_l, dim_source FROM parts WHERE part_number='HYD-001'")[0]
    assert part["dim_l"] is None

    crawler.accept_result(cand["id"])
    part = _q("SELECT * FROM parts WHERE part_number='HYD-001'")[0]
    assert part["dim_l"] == 24.0 and part["dim_source"] == "crawl"
    assert part["weight"] == 3.0
    assert crawler.list_results() == []


def test_crawl_uses_cache_on_second_run(fake_site):
    imports.import_parts([{"part": "HYD-001",
                           "url": "https://supplier.example/p/HYD-001"}])
    crawler.crawl_part("HYD-001", min_interval_s=0)
    assert fake_site["fetch"] == 1
    crawler.crawl_part("HYD-001", min_interval_s=0)
    assert fake_site["fetch"] == 1  # served from crawl_cache


def test_crawl_respects_robots(monkeypatch):
    imports.import_parts([{"part": "X-1", "url": "https://blocked.example/x"}])
    monkeypatch.setattr(crawler, "_robots_allowed", lambda url, cn: False)
    monkeypatch.setattr(crawler, "_fetch",
                        lambda url, timeout=20.0: pytest.fail("must not fetch"))
    out = crawler.crawl_part("X-1", min_interval_s=0)
    assert out["results"] == 0


def test_crawl_disabled_by_setting(monkeypatch):
    from stockopoly import settings
    settings.put("crawl_live", False)
    out = crawler.crawl_part("missing-part-ok")
    assert out["status"] == "disabled"


def test_crawl_pending_targets_dimless_parts(fake_site):
    imports.import_parts([
        {"part": "HYD-001", "url": "https://supplier.example/p/HYD-001"},
        {"part": "HASDIMS", "url": "https://supplier.example/p/HASDIMS",
         "length": 1, "width": 1, "height": 1},
    ])
    results = crawler.crawl_pending(min_interval_s=0)
    assert [r["part_number"] for r in results] == ["HYD-001"]
