"""Grouping cascade: tier-2 heuristics, tier-1 SCB knowledge, manual CRUD."""
from __future__ import annotations

import json
import sqlite3

from stockopoly import grouping, intake, settings
from stockopoly.grouping.heuristics import haversine_m
from stockopoly.store import open_conn
from tests.helpers import build_exif_tiff, build_jpeg, make_bundle_zip


def _loose_batch_with_two_scenes(tmp_path):
    """Four photos: two shot together at site A, two at site B an hour later."""
    src = tmp_path / "shots"
    src.mkdir()
    scenes = [
        ("a1.jpg", "2026:06:01 10:00:00", (35, 30, 0.0)),
        ("a2.jpg", "2026:06:01 10:00:20", (35, 30, 0.1)),
        ("b1.jpg", "2026:06:01 11:00:00", (35, 30, 18.0)),  # ~550 m away
        ("b2.jpg", "2026:06:01 11:00:30", (35, 30, 18.1)),
    ]
    for name, dt, lat in scenes:
        (src / name).write_bytes(build_jpeg(
            640, 480, exif=build_exif_tiff(datetime_original=dt, lat=lat)))
    return intake.ingest_loose(src)["batch_id"]


def test_haversine_sanity():
    assert haversine_m(35.5, -82.26, 35.5, -82.26) == 0.0
    d = haversine_m(35.5, -82.26, 35.5009, -82.26)  # ~0.0009° lat ≈ 100 m
    assert 90 < d < 110


def test_tier2_time_gps_clustering(tmp_path):
    batch_id = _loose_batch_with_two_scenes(tmp_path)
    result = grouping.run_cascade(batch_id)
    assert result["tiers_run"] == [2]
    same_obj = [g for g in result["groups"] if g["kind"] == "same_object"]
    assert len(same_obj) == 2
    sets = sorted(tuple(sorted(p.split("::")[1] for p in g["photo_ids"]))
                  for g in same_obj)
    assert sets == [("a1.jpg", "a2.jpg"), ("b1.jpg", "b2.jpg")]
    assert all(g["source_tier"] == 2 and not g["confirmed"] for g in same_obj)


def test_bundle_preset_groups_whole_batch(tmp_path):
    summary = intake.ingest_bundle(make_bundle_zip(tmp_path, "sess-grp", n_photos=3))
    result = grouping.run_cascade(summary["batch_id"])
    bundle_groups = [g for g in result["groups"]
                     if g["meta"].get("signal") == "bundle_preset"]
    assert len(bundle_groups) == 1
    assert len(bundle_groups[0]["photo_ids"]) == 3
    assert bundle_groups[0]["confidence"] == 0.9


def test_filename_hint_scale_reference(tmp_path):
    src = tmp_path / "with_ref"
    src.mkdir()
    (src / "ruler_01.jpg").write_bytes(build_jpeg())
    (src / "shelf.jpg").write_bytes(build_jpeg())
    batch_id = intake.ingest_loose(src)["batch_id"]
    result = grouping.run_cascade(batch_id)
    refs = [g for g in result["groups"] if g["kind"] == "scale_reference"]
    assert len(refs) == 1
    assert refs[0]["photo_ids"][0].endswith("ruler_01.jpg")


def test_cascade_preserves_manual_and_confirmed(tmp_path):
    batch_id = _loose_batch_with_two_scenes(tmp_path)
    first = grouping.run_cascade(batch_id)
    auto = [g for g in first["groups"] if g["kind"] == "same_object"][0]
    grouping.confirm_group(auto["group_id"])
    photos = intake.batch_photos(batch_id)
    manual_gid = grouping.create_group(
        batch_id, "location_label", "Aisle sign", [photos[0]["photo_id"]])

    second = grouping.run_cascade(batch_id)
    gids = {g["group_id"] for g in second["groups"]}
    assert auto["group_id"] in gids       # confirmed survives
    assert manual_gid in gids             # manual survives
    confirmed = [g for g in second["groups"] if g["group_id"] == auto["group_id"]][0]
    assert confirmed["confirmed"] is True
    # and the cascade didn't duplicate the confirmed cluster
    same_sets = [tuple(g["photo_ids"]) for g in second["groups"]
                 if g["kind"] == "same_object"]
    assert len(same_sets) == len(set(same_sets))


def test_tier1_scb_session_knowledge(tmp_path, monkeypatch, fake_scb):
    # Ingest a bundle locally, then plant matching rows in the fake Brain DB.
    summary = intake.ingest_bundle(make_bundle_zip(tmp_path, "sess-scb1", n_photos=2))
    photos = intake.batch_photos(summary["batch_id"])
    scb_db = fake_scb / "pipeline" / "local_brain.sqlite"
    cn = sqlite3.connect(str(scb_db))
    cn.executescript(
        "CREATE TABLE photogrammetry_photos(photo_key TEXT PRIMARY KEY,"
        " session_id TEXT, sha256 TEXT);"
        "CREATE TABLE photogrammetry_sessions(session_id TEXT PRIMARY KEY,"
        " session_name TEXT);")
    cn.execute("INSERT INTO photogrammetry_sessions VALUES('sess-scb1','Rack A scan')")
    for p in photos:
        cn.execute("INSERT INTO photogrammetry_photos VALUES(?,?,?)",
                   (f"sess-scb1::{p['file']}", "sess-scb1", p["sha256"]))
    cn.commit()
    cn.close()

    settings.put("scb_vision", True)
    result = grouping.run_cascade(summary["batch_id"])
    assert 1 in result["tiers_run"]
    tier1 = [g for g in result["groups"] if g["source_tier"] == 1]
    assert len(tier1) == 1
    assert tier1[0]["label"] == "Rack A scan"
    assert len(tier1[0]["photo_ids"]) == 2
    # tier 2's bundle_preset proposal is covered by tier 1 → no duplicate set
    sets = [tuple(g["photo_ids"]) for g in result["groups"]
            if g["kind"] == "same_object"]
    assert len(sets) == len(set(sets))


def test_tier3_skipped_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    settings.put("llm_vision", True)
    batch_id = _loose_batch_with_two_scenes(tmp_path)
    result = grouping.run_cascade(batch_id)
    assert 3 in result["tiers_run"]
    assert all(g["source_tier"] != 3 for g in result["groups"])


def test_group_crud_and_events(tmp_path):
    batch_id = _loose_batch_with_two_scenes(tmp_path)
    photos = intake.batch_photos(batch_id)
    gid = grouping.create_group(batch_id, "relational_size", "box vs rack",
                                [photos[0]["photo_id"], photos[2]["photo_id"]])
    detail = grouping.group_detail(gid)
    assert detail["confirmed"] == 1 and len(detail["members"]) == 2

    grouping.set_members(gid, [photos[1]["photo_id"]])
    assert len(grouping.group_detail(gid)["members"]) == 1

    grouping.delete_group(gid)
    assert all(g["group_id"] != gid for g in grouping.list_groups(batch_id))

    grouping.run_cascade(batch_id)
    cn = open_conn()
    try:
        kinds = [r["kind"] for r in cn.execute("SELECT kind FROM events")]
    finally:
        cn.close()
    assert "stockopoly_grouping" in kinds
