"""Tier-3 routing: shared vision primitives + Brain delegation vs standalone.

Covers ``vision_common`` (the de-duplicated prompt/encode/parse helpers shared by
both transports), ``scb_dispatch`` (delegation to the Supply-Chain-Brain ensemble
with graceful fallback), and the cascade's preference/fallback wiring.
"""
from __future__ import annotations

import sys

import pytest

from stockopoly import grouping, intake, settings
from stockopoly.grouping import llm, scb_dispatch, vision_common
from tests.helpers import build_exif_tiff, build_jpeg

_STUB_URI = "data:image/jpeg;base64,AAAA"


def _stub_encoding(monkeypatch):
    """Make image encoding deterministic regardless of Pillow availability."""
    monkeypatch.setattr(vision_common, "encode_photo", lambda path: _STUB_URI)


def _two_unclustered_photos(tmp_path):
    """Two loose photos far apart in time + space — tier 2 groups nothing,
    so any tier-3 same_object proposal over both is visible (uncovered)."""
    src = tmp_path / "shots"
    src.mkdir()
    (src / "x.jpg").write_bytes(build_jpeg(
        exif=build_exif_tiff(datetime_original="2026:06:01 10:00:00", lat=(35, 30, 0.0))))
    (src / "y.jpg").write_bytes(build_jpeg(
        exif=build_exif_tiff(datetime_original="2026:06:01 12:00:00", lat=(35, 30, 40.0))))
    return intake.ingest_loose(src)["batch_id"]


@pytest.fixture()
def _restore_brain_import():
    """Undo sys.path / sys.modules mutations from importing a fake Brain."""
    path_before = list(sys.path)
    mods_before = set(sys.modules)
    yield
    for name in list(sys.modules):
        if (name == "brain" or name.startswith("brain.")) and name not in mods_before:
            sys.modules.pop(name, None)
    sys.path[:] = path_before


# ─────────────────────────────────────────────────────────── vision_common
def test_parse_groups_resolves_indices_and_filters_singletons():
    photos = [{"photo_id": "A"}, {"photo_id": "B"}, {"photo_id": "C"}]
    sent_idx = [0, 2]  # photo B was skipped during encoding
    parsed = {"groups": [
        {"label": "rack", "kind": "same_object", "indices": [0, 1], "confidence": 0.9},
        {"label": "lonely", "kind": "same_object", "indices": [1], "confidence": 0.9},
        {"label": "ruler", "kind": "scale_reference", "indices": [0], "confidence": 0.4},
    ]}
    out = vision_common.parse_groups(parsed, photos, sent_idx,
                                     signal="llm_vision", route="scb_dispatch",
                                     model="m1")
    # 0->A, 1->C via sent_idx; singleton same_object dropped; scale_reference kept.
    same = [p for p in out if p["kind"] == "same_object"]
    refs = [p for p in out if p["kind"] == "scale_reference"]
    assert len(same) == 1 and same[0]["photo_ids"] == ["A", "C"]
    assert same[0]["meta"] == {"signal": "llm_vision", "route": "scb_dispatch", "model": "m1"}
    assert len(refs) == 1 and refs[0]["photo_ids"] == ["A"]


def test_build_content_shapes_multimodal_message(monkeypatch):
    _stub_encoding(monkeypatch)
    photos = [{"abs_path": "/x/a.jpg", "file": "a.jpg"},
              {"abs_path": "/x/b.jpg", "file": "b.jpg"}]
    content, sent_idx = vision_common.build_content(photos)
    assert sent_idx == [0, 1]
    assert content[0] == {"type": "text", "text": vision_common.PROMPT}
    images = [c for c in content if c["type"] == "image_url"]
    assert len(images) == 2 and images[0]["image_url"]["url"] == _STUB_URI


def test_extract_json_tolerates_prose():
    assert vision_common.extract_json("noise {\"groups\": []} trailing") == {"groups": []}
    assert vision_common.extract_json("no json here") is None


# ─────────────────────────────────────────────────────────── scb_dispatch
def test_propose_none_when_brain_absent(tmp_path, monkeypatch):
    # Default hermetic env (conftest points SCB_REPO_DIR at a missing dir).
    _stub_encoding(monkeypatch)
    photos = [{"abs_path": "/x/a.jpg", "file": "a.jpg", "photo_id": "p::a"},
              {"abs_path": "/x/b.jpg", "file": "b.jpg", "photo_id": "p::b"}]
    assert scb_dispatch.available() is False
    assert scb_dispatch.propose(photos, {}, {}) is None


def test_propose_none_on_brain_sentinel(monkeypatch):
    _stub_encoding(monkeypatch)
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call",
                        lambda: (lambda messages, task=None: {"content": "[llm_ensemble_call error] boom"}))
    photos = [{"abs_path": "/x/a.jpg", "file": "a.jpg", "photo_id": "p::a"},
              {"abs_path": "/x/b.jpg", "file": "b.jpg", "photo_id": "p::b"}]
    assert scb_dispatch.propose(photos, {}, {}) is None


def test_propose_parses_brain_groups(monkeypatch):
    _stub_encoding(monkeypatch)
    reply = {"content": '{"groups":[{"label":"Rack X","kind":"same_object",'
                        '"indices":[0,1],"confidence":0.82}]}', "model": "fake/vision"}
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call",
                        lambda: (lambda messages, task=None: reply))
    photos = [{"abs_path": "/x/a.jpg", "file": "a.jpg", "photo_id": "p::a"},
              {"abs_path": "/x/b.jpg", "file": "b.jpg", "photo_id": "p::b"}]
    out = scb_dispatch.propose(photos, {}, {})
    assert out and out[0]["kind"] == "same_object"
    assert out[0]["photo_ids"] == ["p::a", "p::b"]
    assert out[0]["meta"]["route"] == "scb_dispatch"
    assert out[0]["meta"]["model"] == "fake/vision"


# ──────────────────────────────────────────────── cascade preference + fallback
def test_cascade_uses_real_brain_import(tmp_path, monkeypatch, fake_scb, _restore_brain_import):
    """End-to-end: real sibling discovery + import of an on-disk fake Brain."""
    _stub_encoding(monkeypatch)
    brain_dir = fake_scb / "pipeline" / "src" / "brain"
    brain_dir.mkdir(parents=True)
    (brain_dir / "__init__.py").write_text("")
    (brain_dir / "llm_ensemble.py").write_text(
        "def llm_ensemble_call(messages, task='default', model=None):\n"
        "    return {'content': '{\"groups\":[{\"label\":\"Brain rack\","
        "\"kind\":\"same_object\",\"indices\":[0,1],\"confidence\":0.8}]}',\n"
        "            'model': 'fake/vision-1'}\n"
    )
    batch_id = _two_unclustered_photos(tmp_path)
    settings.put("llm_vision", True)

    result = grouping.run_cascade(batch_id)

    assert 3 in result["tiers_run"]
    tier3 = [g for g in result["groups"] if g["source_tier"] == 3]
    assert len(tier3) == 1
    assert tier3[0]["meta"]["route"] == "scb_dispatch"
    assert tier3[0]["meta"]["model"] == "fake/vision-1"
    assert len(tier3[0]["photo_ids"]) == 2


def test_cascade_falls_back_to_direct_when_brain_returns_none(tmp_path, monkeypatch):
    batch_id = _two_unclustered_photos(tmp_path)
    photos = intake.batch_photos(batch_id)
    pids = sorted(p["photo_id"] for p in photos)

    monkeypatch.setattr(scb_dispatch, "propose", lambda *a, **k: None)
    direct_called = {"n": 0}

    def fake_direct(ph, batch, cfg):
        direct_called["n"] += 1
        return [{"kind": "same_object", "label": "DIRECT", "confidence": 0.7,
                 "photo_ids": pids, "meta": {"route": "openrouter_direct"}}]

    monkeypatch.setattr(llm, "propose", fake_direct)
    settings.put("llm_vision", True)

    result = grouping.run_cascade(batch_id)
    tier3 = [g for g in result["groups"] if g["source_tier"] == 3]
    assert direct_called["n"] == 1
    assert len(tier3) == 1 and tier3[0]["label"] == "DIRECT"
    assert tier3[0]["meta"]["route"] == "openrouter_direct"


def test_cascade_prefers_brain_and_skips_direct(tmp_path, monkeypatch):
    batch_id = _two_unclustered_photos(tmp_path)
    photos = intake.batch_photos(batch_id)
    pids = sorted(p["photo_id"] for p in photos)

    monkeypatch.setattr(scb_dispatch, "propose", lambda *a, **k: [
        {"kind": "same_object", "label": "SCB", "confidence": 0.8,
         "photo_ids": pids, "meta": {"route": "scb_dispatch"}}])

    def must_not_run(*a, **k):
        raise AssertionError("direct OpenRouter must not run when the Brain answered")

    monkeypatch.setattr(llm, "propose", must_not_run)
    settings.put("llm_vision", True)

    result = grouping.run_cascade(batch_id)
    tier3 = [g for g in result["groups"] if g["source_tier"] == 3]
    assert len(tier3) == 1 and tier3[0]["label"] == "SCB"


def test_cascade_respects_prefer_scb_off(tmp_path, monkeypatch):
    batch_id = _two_unclustered_photos(tmp_path)

    def must_not_run(*a, **k):
        raise AssertionError("scb_dispatch must not run when llm_vision_prefer_scb is off")

    monkeypatch.setattr(scb_dispatch, "propose", must_not_run)
    monkeypatch.setattr(llm, "propose", lambda *a, **k: [])
    settings.put("llm_vision", True)
    settings.put("llm_vision_prefer_scb", False)

    result = grouping.run_cascade(batch_id)  # direct path only; no error
    assert 3 in result["tiers_run"]


# ────────────────────────────────────────── recall cache (cheap before costed)
class _FakeCache:
    """In-memory stand-in for the Brain's vlm_cache module."""

    def __init__(self):
        self.store: dict = {}
        self.remembered: list = []

    def recall(self, key, *, kind, phash=None, max_hamming=6):
        val = self.store.get((key, kind))
        if val is None:
            return None
        return {"result": val, "cached": True, "source": "exact",
                "model": "cached/model", "cost_tier": "costed", "confidence": None}

    def remember(self, key, result, *, kind, phash=None, model=None,
                 cost_tier="costed", confidence=None):
        self.store[(key, kind)] = result
        self.remembered.append((key, kind, result, model))
        return True


def _two_photos_with_sha():
    return [{"abs_path": "/x/a.jpg", "file": "a.jpg", "photo_id": "b::a", "sha256": "sha_a"},
            {"abs_path": "/x/b.jpg", "file": "b.jpg", "photo_id": "b::b", "sha256": "sha_b"}]


def _boom_loader():
    raise AssertionError("ensemble must not be loaded on a cache hit")


def test_dispatch_recalls_cached_grouping(monkeypatch):
    _stub_encoding(monkeypatch)
    subset = _two_photos_with_sha()
    cache = _FakeCache()
    key = scb_dispatch._content_key(subset)
    cache.store[(key, scb_dispatch._CACHE_KIND)] = {"groups": [
        {"shas": ["sha_a", "sha_b"], "kind": "same_object", "label": "cached rack",
         "confidence": 0.8, "meta": {"signal": "llm_vision"}}]}
    monkeypatch.setattr(scb_dispatch, "_load_vlm_cache", lambda: cache)
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call", _boom_loader)

    out = scb_dispatch.propose(subset, {}, {})
    assert out and out[0]["photo_ids"] == ["b::a", "b::b"]
    assert out[0]["label"] == "cached rack"
    assert out[0]["meta"]["route"] == "scb_cache" and out[0]["meta"]["cached"] is True


def test_dispatch_remembers_after_miss_then_recalls(monkeypatch):
    _stub_encoding(monkeypatch)
    subset = _two_photos_with_sha()
    cache = _FakeCache()
    monkeypatch.setattr(scb_dispatch, "_load_vlm_cache", lambda: cache)
    reply = {"content": '{"groups":[{"label":"Rack","kind":"same_object",'
                        '"indices":[0,1],"confidence":0.82}]}', "model": "fake/v"}
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call",
                        lambda: (lambda messages, task=None: reply))

    out = scb_dispatch.propose(subset, {}, {})
    assert out[0]["photo_ids"] == ["b::a", "b::b"] and out[0]["meta"]["route"] == "scb_dispatch"
    # written back as sha-keyed groups (reusable across batches/photo_ids)
    assert len(cache.remembered) == 1
    _, kind, result, model = cache.remembered[0]
    assert kind == scb_dispatch._CACHE_KIND and model == "fake/v"
    assert result["groups"][0]["shas"] == ["sha_a", "sha_b"]

    # second identical request now recalls — ensemble must not run
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call", _boom_loader)
    out2 = scb_dispatch.propose(subset, {}, {})
    assert out2[0]["meta"]["route"] == "scb_cache" and out2[0]["photo_ids"] == ["b::a", "b::b"]


def test_dispatch_cache_disabled_by_setting(monkeypatch):
    _stub_encoding(monkeypatch)
    subset = _two_photos_with_sha()
    loads = {"n": 0}

    def counting_loader():
        loads["n"] += 1
        return _FakeCache()

    monkeypatch.setattr(scb_dispatch, "_load_vlm_cache", counting_loader)
    reply = {"content": '{"groups":[{"label":"R","kind":"same_object",'
                        '"indices":[0,1],"confidence":0.8}]}', "model": "m"}
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call",
                        lambda: (lambda messages, task=None: reply))

    out = scb_dispatch.propose(subset, {}, {"scb_vlm_cache": False})
    assert loads["n"] == 0  # cache never consulted when disabled
    assert out[0]["meta"]["route"] == "scb_dispatch"


def test_dispatch_skips_cache_without_sha(monkeypatch):
    _stub_encoding(monkeypatch)
    subset = [{"abs_path": "/x/a.jpg", "file": "a.jpg", "photo_id": "b::a"},
              {"abs_path": "/x/b.jpg", "file": "b.jpg", "photo_id": "b::b"}]  # no sha256
    cache = _FakeCache()
    monkeypatch.setattr(scb_dispatch, "_load_vlm_cache", lambda: cache)
    reply = {"content": '{"groups":[{"label":"R","kind":"same_object",'
                        '"indices":[0,1],"confidence":0.8}]}', "model": "m"}
    monkeypatch.setattr(scb_dispatch, "_load_ensemble_call",
                        lambda: (lambda messages, task=None: reply))

    out = scb_dispatch.propose(subset, {}, {})
    assert out[0]["meta"]["route"] == "scb_dispatch"
    assert cache.remembered == []  # no content key → nothing cached
