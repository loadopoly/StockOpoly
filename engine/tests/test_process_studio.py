"""v-ADMIN staging client for QUIPU's x_ProcessStudio holon: edge-canonical
signing, safety-net dispatch, VLM-shaped proposals, cascade wiring."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import urllib.error

import pytest

from stockopoly import grouping, intake, process_studio, settings
from stockopoly.grouping import scb_dispatch
from tests.helpers import build_exif_tiff, build_jpeg

KEY = "ab" * 32


class _FakeNet:
    """Stands in for QuipuSafetyNet: records execute/flush calls."""

    def __init__(self, result=None):
        self.result = result if result is not None else {
            "ok": True, "safety_net": "direct_pass",
            "response": {"ok": True, "held": True,
                         "proposal": {"proposal_id": "act_abc123"}}}
        self.calls = []

    def execute(self, target_url, payload, dispatcher):
        self.calls.append((target_url, payload, dispatcher))
        return dict(self.result)

    def flush(self, dispatcher, max_batch=50):
        self.calls.append(("flush", dispatcher, max_batch))
        return {"ok": True, "drained": 0, "remaining": 0}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ───────────────────────────────────────────────── signing (edge canonical)
def test_sign_headers_matches_quipu_edge_canonical_form():
    body = b'{"source":"stockopoly"}'
    idem = process_studio.idempotency_key("/process_studio/propose", body)
    h = process_studio.sign_headers(KEY, "POST", "/process_studio/propose",
                                    body, idem=idem, ts=1700000000)
    msg = ("POST\n/process_studio/propose\n1700000000\n"
           + hashlib.sha256(body).hexdigest() + "\n" + idem)
    want = hmac.new(bytes.fromhex(KEY), msg.encode(), hashlib.sha256).hexdigest()
    assert h["X-Quipu-Signature"] == want
    assert h["X-Quipu-Source"] == "stockopoly" and h["X-Quipu-Idempotency"] == idem
    assert process_studio.idempotency_key("/process_studio/propose", body) == idem


def test_dispatch_signs_when_key_present(monkeypatch):
    monkeypatch.setenv("QUIPU_EDGE_KEY_STOCKOPOLY", KEY)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["headers"] = {k.title(): v for k, v in req.header_items()}
        seen["body"] = req.data
        return _Resp(b'{"ok": true, "held": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, res = process_studio._dispatch(
        "http://quipu:7100/process_studio/propose", {"source": "stockopoly", "tool": "t"})
    assert ok is True and res == {"ok": True, "held": True}
    hdr = seen["headers"]
    assert hdr["X-Quipu-Source"] == "stockopoly"
    idem = hdr["X-Quipu-Idempotency"]
    msg = ("POST\n/process_studio/propose\n" + hdr["X-Quipu-Timestamp"] + "\n"
           + hashlib.sha256(seen["body"]).hexdigest() + "\n" + idem)
    want = hmac.new(bytes.fromhex(KEY), msg.encode(), hashlib.sha256).hexdigest()
    assert hdr["X-Quipu-Signature"] == want           # verifies against the edge


def test_dispatch_unsigned_without_key(monkeypatch):
    monkeypatch.delenv("QUIPU_EDGE_KEY_STOCKOPOLY", raising=False)
    monkeypatch.delenv("QUIPU_EDGE_KEY", raising=False)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["headers"] = {k.title(): v for k, v in req.header_items()}
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, _ = process_studio._dispatch("http://quipu:7100/process_studio/propose",
                                     {"source": "stockopoly"})
    assert ok and "X-Quipu-Signature" not in seen["headers"]
    assert seen["headers"]["X-Quipu-Source"] == "stockopoly"
    assert seen["headers"]["X-Quipu-Idempotency"]      # dedupe still rides along


def test_dispatch_reports_http_and_network_failures(monkeypatch):
    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", None,
                                     io.BytesIO(b'{"error":"no grant"}'))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    ok, err = process_studio._dispatch("http://quipu:7100/process_studio/propose", {})
    assert ok is False and err.startswith("HTTP 403") and "no grant" in err

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(OSError("down")))
    ok, err = process_studio._dispatch("http://quipu:7100/process_studio/propose", {})
    assert ok is False and "down" in err


# ───────────────────────────────────────────────── staging (v-ADMIN only)
def test_propose_action_stages_through_the_net():
    net = _FakeNet()
    out = process_studio.propose_action("start_flow", {"flow": "replenish"},
                                        reason="low stock", net=net)
    assert out["staged"] is True and out["proposal_id"] == "act_abc123"
    target, payload, dispatcher = net.calls[0]
    assert target.endswith("/process_studio/propose")
    assert payload["source"] == "stockopoly" and payload["tool"] == "start_flow"
    assert payload["meta"]["schema"] == process_studio.SCHEMA
    assert dispatcher is process_studio._dispatch


def test_propose_action_spooled_when_quipu_unreachable():
    net = _FakeNet(result={"ok": True, "spooled": True,
                           "safety_net": "diverted_on_failure"})
    out = process_studio.propose_action("t", net=net)
    assert out["staged"] is False and out["spooled"] is True and out["ok"] is True
    assert process_studio.propose_action("", net=net)["staged"] is False


def test_propose_regroup_shapes_the_vlm_evidence():
    net = _FakeNet()
    proposals = [{"kind": "same_object", "label": "rack", "confidence": 0.91,
                  "photo_ids": ["a", "b"]},
                 {"kind": "scale_reference", "label": "ruler", "confidence": 0.4,
                  "photo_ids": ["a"]}]
    out = process_studio.propose_regroup("batch-12345678", proposals, net=net)
    assert out["staged"] is True
    _, payload, _ = net.calls[0]
    vlm = payload["meta"]["vlm"]
    assert vlm["proposals"] == 2 and vlm["confidence_max"] == 0.91
    assert vlm["kinds"] == {"same_object": 1, "scale_reference": 1}
    assert "batch-12" in payload["reason"]


def test_flush_rides_the_net():
    net = _FakeNet()
    assert process_studio.flush(net=net)["ok"] is True
    assert net.calls[0][0] == "flush"


# ───────────────────────────────────────────────── cascade wiring (tier 3)
def _loose_batch(tmp_path):
    src = tmp_path / "shots"
    src.mkdir()
    (src / "x.jpg").write_bytes(build_jpeg(
        exif=build_exif_tiff(datetime_original="2026:06:01 10:00:00", lat=(35, 30, 0.0))))
    (src / "y.jpg").write_bytes(build_jpeg(
        exif=build_exif_tiff(datetime_original="2026:06:01 12:00:00", lat=(35, 30, 40.0))))
    return intake.ingest_loose(src)["batch_id"]


def test_cascade_stages_studio_proposal_when_enabled(tmp_path, monkeypatch):
    batch_id = _loose_batch(tmp_path)
    from stockopoly.store import open_conn
    cn = open_conn()
    try:
        pids = [r["photo_id"] for r in grouping._batch_photos(cn, batch_id)]
    finally:
        cn.close()
    settings.put("llm_vision", True)
    settings.put("process_studio_propose", True)
    monkeypatch.setattr(scb_dispatch, "propose",
                        lambda photos, batch, cfg: [{"kind": "same_object", "label": "rack",
                                                     "confidence": 0.9, "photo_ids": pids}])
    staged = []
    monkeypatch.setattr(process_studio, "propose_regroup",
                        lambda bid, props, **kw: staged.append((bid, len(props))) or {"ok": True})
    out = grouping.run_cascade(batch_id)
    assert 3 in out["tiers_run"] and staged == [(batch_id, 1)]


def test_cascade_does_not_stage_by_default(tmp_path, monkeypatch):
    batch_id = _loose_batch(tmp_path)
    settings.put("llm_vision", True)
    monkeypatch.setattr(scb_dispatch, "propose", lambda photos, batch, cfg: [])
    called = []
    monkeypatch.setattr(process_studio, "propose_regroup",
                        lambda *a, **k: called.append(1))
    grouping.run_cascade(batch_id)
    assert called == []                                # off by default; empty tier-3 too
