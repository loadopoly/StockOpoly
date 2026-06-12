"""Directed supplier-dimension crawler — polite by construction.

For parts that carry a ``supplier_url`` (or match a configured
``supplier_sites`` URL pattern), fetch the product page and regex-extract
"L × W × H" dimension triples and weights. Candidates land in
``crawl_results`` with a provenance excerpt; **nothing touches the part
record until a human calls** :func:`accept_result`.

Politeness: robots.txt honoured per domain (stdlib robotparser), one fetch
per ``min_interval_s`` per domain, responses cached in ``crawl_cache`` so a
re-crawl within ``cache_ttl_h`` never re-hits the site. ``requests`` is used
when installed, else urllib. The ``crawl_live`` setting AND an explicit call
are both required — there is no background crawling.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.robotparser
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .. import events, settings
from ..store import open_conn

__all__ = ["extract_dims", "extract_weight", "crawl_part", "crawl_pending",
           "accept_result", "reject_result", "list_results"]

USER_AGENT = "StockOpolyBot/0.1 (local warehouse tool; one-shot product lookup)"
_MIN_INTERVAL_S = 5.0
_CACHE_TTL_H = 24 * 7
_last_fetch: dict[str, float] = {}

_UNIT = r'(?:["″]|\s*(?:in|inch|inches|cm|mm)\b\.?)'
_NUM = r"(\d+(?:\.\d+)?)"
_DIM_TRIPLE = re.compile(
    _NUM + r"\s*" + _UNIT + r"?\s*[x×X]\s*"
    + _NUM + r"\s*" + _UNIT + r"?\s*[x×X]\s*"
    + _NUM + r"\s*(\"|″|in\b|inch\b|inches\b|cm\b|mm\b)?", re.I)
_WEIGHT = re.compile(_NUM + r"\s*(lbs?|pounds?|kg)\b", re.I)
_DIM_KEYWORD = re.compile(r"dimension|size|\bL\s*[x×]\s*W\s*[x×]\s*H\b", re.I)

_TO_IN = {"cm": 1 / 2.54, "mm": 1 / 25.4, None: 1.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_dims(text: str) -> list[dict[str, Any]]:
    """All L×W×H candidates in a blob of page text, converted to inches."""
    out = []
    for m in _DIM_TRIPLE.finditer(text):
        unit_raw = (m.group(4) or "").lower().strip(". ")
        if unit_raw in ("\"", "″", "in", "inch", "inches"):
            unit = None  # inches
        elif unit_raw in ("cm", "mm"):
            unit = unit_raw
        else:
            unit = None
        factor = _TO_IN[unit]
        dims = [round(float(m.group(i)) * factor, 3) for i in (1, 2, 3)]
        if not all(0.05 <= d <= 1200 for d in dims):
            continue
        start, end = max(m.start() - 80, 0), min(m.end() + 80, len(text))
        excerpt = " ".join(text[start:end].split())
        has_kw = bool(_DIM_KEYWORD.search(text[start:end]))
        has_unit = bool(m.group(4))
        confidence = 0.8 if (has_kw and has_unit) else 0.6 if has_unit else 0.4
        out.append({"dim_l": dims[0], "dim_w": dims[1], "dim_h": dims[2],
                    "confidence": confidence, "excerpt": excerpt})
    out.sort(key=lambda d: -d["confidence"])
    return out


def extract_weight(text: str) -> float | None:
    best = None
    for m in _WEIGHT.finditer(text):
        v = float(m.group(1))
        if m.group(2).lower().startswith("kg"):
            v *= 2.20462
        if 0.01 <= v <= 20000 and best is None:
            best = round(v, 3)
    return best


def _robots_allowed(url: str, cn) -> bool:
    domain = urlparse(url).netloc
    row = cn.execute("SELECT robots_ok FROM crawl_cache WHERE url=?",
                     (f"robots://{domain}",)).fetchone()
    if row is not None:
        return bool(row["robots_ok"])
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{urlparse(url).scheme}://{domain}/robots.txt")
    try:
        rp.read()
        allowed = rp.can_fetch(USER_AGENT, url)
    except Exception:
        allowed = True  # unreachable robots.txt → default-allow, still rate-limited
    cn.execute("INSERT OR REPLACE INTO crawl_cache(url, domain, fetched_at,"
               " status, robots_ok) VALUES (?,?,?,0,?)",
               (f"robots://{domain}", domain, _now(), 1 if allowed else 0))
    cn.commit()
    return allowed


def _fetch(url: str, timeout: float = 20.0) -> tuple[int, str]:
    """GET a page; requests when available, urllib otherwise."""
    try:
        import requests  # type: ignore
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        return resp.status_code, resp.text[:500_000]
    except ImportError:
        pass
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(500_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _cached_page(url: str, cn) -> str | None:
    row = cn.execute("SELECT fetched_at, status, content_excerpt FROM crawl_cache"
                     " WHERE url=?", (url,)).fetchone()
    if row is None or row["status"] != 200:
        return None
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(row["fetched_at"])).total_seconds() / 3600
    except ValueError:
        return None
    return row["content_excerpt"] if age_h <= _CACHE_TTL_H else None


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _candidate_urls(part, cfg) -> list[str]:
    urls = []
    if part["supplier_url"]:
        urls.append(part["supplier_url"])
    for site in cfg.get("supplier_sites") or []:
        if part["supplier_name"] and site.get("supplier") and \
                site["supplier"].lower() not in part["supplier_name"].lower():
            continue
        pattern = site.get("url_pattern") or ""
        if "{part}" in pattern or "{supplier_part}" in pattern:
            urls.append(pattern
                        .replace("{part}", part["part_number"])
                        .replace("{supplier_part}",
                                 part["supplier_part_number"] or part["part_number"]))
    return urls


def crawl_part(part_number: str, *, min_interval_s: float = _MIN_INTERVAL_S
               ) -> dict[str, Any]:
    """Crawl one part's candidate URLs → crawl_results rows (pending accept)."""
    cfg = settings.all_settings()
    if not cfg.get("crawl_live"):
        return {"part_number": part_number, "status": "disabled", "results": 0}
    cn = open_conn()
    try:
        part = cn.execute(
            "SELECT part_number, supplier_name, supplier_part_number,"
            " supplier_url FROM parts WHERE part_number=?",
            (part_number,)).fetchone()
        if part is None:
            raise KeyError(f"Unknown part {part_number}")
        urls = _candidate_urls(part, cfg)
        if not urls:
            return {"part_number": part_number, "status": "no_url", "results": 0}

        found = 0
        for url in urls[:3]:
            domain = urlparse(url).netloc
            text = _cached_page(url, cn)
            from_cache = text is not None
            if text is None:
                if not _robots_allowed(url, cn):
                    continue
                wait = _last_fetch.get(domain, 0.0) + min_interval_s - time.time()
                if wait > 0:
                    time.sleep(wait)
                _last_fetch[domain] = time.time()
                status, html = _fetch(url)
                text = _strip_html(html)
                cn.execute(
                    "INSERT OR REPLACE INTO crawl_cache(url, domain, fetched_at,"
                    " status, content_hash, content_excerpt, robots_ok)"
                    " VALUES (?,?,?,?,?,?,1)",
                    (url, domain, _now(), status,
                     hashlib.sha256(text.encode()).hexdigest()[:16], text[:20_000]))
                cn.commit()
                if status != 200:
                    continue
            weight = extract_weight(text)
            for cand in extract_dims(text)[:3]:
                cn.execute(
                    "INSERT INTO crawl_results(part_number, url, dim_l, dim_w,"
                    " dim_h, weight, unit, weight_unit, method, confidence,"
                    " provenance_excerpt, accepted, created_at)"
                    " VALUES (?,?,?,?,?,?,'in','lb','regex',?,?,0,?)",
                    (part_number, url, cand["dim_l"], cand["dim_w"], cand["dim_h"],
                     weight, cand["confidence"], cand["excerpt"], _now()))
                found += 1
            cn.commit()
            if found and from_cache:
                break
    finally:
        cn.close()
    summary = {"part_number": part_number, "status": "ok", "results": found}
    events.record("stockopoly_crawl", summary,
                  title=f"Crawl {part_number}: {found} candidate(s)",
                  signal=0.5 if found else 0.2)
    return summary


def crawl_pending(limit: int = 10, **kw) -> list[dict[str, Any]]:
    """Crawl parts that have a URL but no dims and no pending candidates."""
    cn = open_conn()
    try:
        parts = [r["part_number"] for r in cn.execute(
            "SELECT p.part_number FROM parts p WHERE p.dim_l IS NULL"
            " AND p.supplier_url IS NOT NULL AND NOT EXISTS"
            " (SELECT 1 FROM crawl_results c WHERE c.part_number=p.part_number"
            "  AND c.accepted=0) LIMIT ?", (limit,))]
    finally:
        cn.close()
    return [crawl_part(pn, **kw) for pn in parts]


def accept_result(result_id: int) -> dict[str, Any]:
    """Human approval: copy candidate dims onto the part record."""
    cn = open_conn()
    try:
        r = cn.execute("SELECT * FROM crawl_results WHERE id=?",
                       (result_id,)).fetchone()
        if r is None:
            raise KeyError(f"Unknown crawl result {result_id}")
        cn.execute(
            "UPDATE parts SET dim_l=?, dim_w=?, dim_h=?,"
            " weight=COALESCE(?, weight), dim_source='crawl', dim_confidence=?,"
            " updated_at=? WHERE part_number=?",
            (r["dim_l"], r["dim_w"], r["dim_h"], r["weight"], r["confidence"],
             _now(), r["part_number"]))
        cn.execute("UPDATE crawl_results SET accepted=1 WHERE id=?", (result_id,))
        cn.execute("UPDATE crawl_results SET accepted=-1 WHERE part_number=?"
                   " AND id != ? AND accepted=0", (r["part_number"], result_id))
        cn.commit()
        return {"part_number": r["part_number"], "dim_l": r["dim_l"],
                "dim_w": r["dim_w"], "dim_h": r["dim_h"]}
    finally:
        cn.close()


def reject_result(result_id: int) -> None:
    cn = open_conn()
    try:
        cn.execute("UPDATE crawl_results SET accepted=-1 WHERE id=?", (result_id,))
        cn.commit()
    finally:
        cn.close()


def list_results(pending_only: bool = True) -> list[dict[str, Any]]:
    cn = open_conn()
    try:
        sql = "SELECT * FROM crawl_results"
        if pending_only:
            sql += " WHERE accepted=0"
        return [dict(r) for r in cn.execute(sql + " ORDER BY id DESC")]
    finally:
        cn.close()
