"""HTTP surface: status, Brain-compatible intake, imports, settings, photos."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from stockopoly import server as server_mod
from tests.helpers import make_bundle_zip


@pytest.fixture()
def api(monkeypatch):
    httpd = server_mod.serve(port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    def call(method, path, *, body=None, ctype="application/json", raw=False):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=data, method=method,
            headers={"Content-Type": ctype} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = resp.read()
                return resp.status, payload if raw else json.loads(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, payload if raw else json.loads(payload)

    call.port = port
    yield call
    httpd.shutdown()
    httpd.server_close()


def _multipart(fields: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "testboundary123"
    out = b""
    for name, filename, payload in fields:
        out += f"--{boundary}\r\n".encode()
        disp = f'form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        out += f"Content-Disposition: {disp}\r\n\r\n".encode()
        out += payload + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def test_status_endpoint(api):
    status, body = api("GET", "/api/status")
    assert status == 200
    assert body["api"] == "stockopoly.api/1"
    assert "batches" in body["counts"]


def test_head_probe(api):
    # the Operate Console pings HEAD / before uplinking
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=5)
    try:
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Access-Control-Allow-Origin") == "*"
    finally:
        conn.close()


def test_intake_multipart_and_duplicate(api, tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-http1", n_photos=2)
    body, ctype = _multipart([
        ("bundle", "bundle.zip", zip_path.read_bytes()),
        ("session", "", b"sess-http1"),
    ])
    status, out = api("POST", "/intake", body=body, ctype=ctype)
    assert status == 200 and out["ok"] is True
    assert out["batch_id"] == "sess-http1" and out["photo_count"] == 2

    status, out = api("POST", "/intake", body=body, ctype=ctype)
    assert status == 200 and out.get("duplicate") is True

    status, batches = api("GET", "/api/batches")
    assert len(batches["batches"]) == 1


def test_intake_raw_zip_and_invalid(api, tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-http2")
    status, out = api("POST", "/intake", body=zip_path.read_bytes(),
                      ctype="application/zip")
    assert status == 200 and out["batch_id"] == "sess-http2"

    status, out = api("POST", "/intake", body=b"not a zip",
                      ctype="application/zip")
    assert status == 422 and "Invalid bundle" in out["error"]


def test_photo_serving_and_traversal_guard(api, tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-http3", n_photos=1)
    api("POST", "/intake", body=zip_path.read_bytes(), ctype="application/zip")
    status, body = api(
        "GET", "/api/photos/sess-http3/photos%2FIMG_0000.jpg", raw=True)
    assert status == 200 and body[:2] == b"\xff\xd8"
    status, out = api("GET", "/api/photos/sess-http3/photos%2Fnope.jpg")
    assert status == 404


def test_import_rows_settings_roundtrip_and_404(api):
    status, out = api("POST", "/api/import/inventory", body={
        "rows": [{"part": "API-1", "location": "A-01-1", "qty": 5}]})
    assert status == 200 and out["imported"] == 1

    status, out = api("PUT", "/api/settings", body={"fill_factor": 0.7})
    assert status == 200 and out["fill_factor"] == 0.7

    status, out = api("GET", "/api/nope")
    assert status == 404

    status, out = api("POST", "/api/import/bogus", body={"rows": []})
    assert status == 404


def test_grouping_and_slotting_flow_over_http(api, tmp_path):
    zip_path = make_bundle_zip(tmp_path, "sess-http4", n_photos=3)
    api("POST", "/intake", body=zip_path.read_bytes(), ctype="application/zip")
    status, out = api("POST", "/api/groups/cascade",
                      body={"batch_id": "sess-http4"})
    assert status == 200 and out["group_count"] >= 1

    api("POST", "/api/import/inventory", body={"rows": [
        {"part": "API-FAST", "location": "A-01-1", "qty": 5},
        {"part": "API-SLOW", "location": "A-02-1", "qty": 5}]})
    api("POST", "/api/import/usage", body={"rows": [
        {"part": "API-FAST", "period": "2026-05", "qty": 50}]})
    api("POST", "/api/locations/coordinates")
    api("POST", "/api/slotting/velocity")
    status, plan = api("POST", "/api/slotting/optimize", body={})
    assert status == 200 and plan["parts_assigned"] == 2
    status, out = api("GET", f"/api/slotting/plans/{plan['plan_id']}/assignments")
    assert status == 200 and len(out["assignments"]) >= 2


def test_root_serves_ui_or_hint(api):
    # With app/dist built, "/" serves the SPA index.html; without a build it
    # returns a JSON service hint. Both name the service, so accept either.
    status, body = api("GET", "/", raw=True)
    assert status == 200
    assert "stockopoly" in body.decode("utf-8", "replace").lower()
