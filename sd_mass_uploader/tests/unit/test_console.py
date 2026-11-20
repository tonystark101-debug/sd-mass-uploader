"""The Operator Console's server side: static files, security headers, Setup endpoints, sign-in."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import src.api.app as app_module
from src.api.app import ApiServices, create_app
from src.cli.commands import main
from tests.unit.peakai_fakes import KEY, make_pack
from tests.unit.test_station import StationHarness

HEADERS = {"Authorization": "Bearer t"}


@pytest.fixture
def harness(tmp_path):
    h = StationHarness(tmp_path)
    yield h
    h.close()


def client_for(harness, settings=True):
    services = ApiServices(harness.station, "t", settings=harness.settings if settings else None)
    return TestClient(create_app(services), base_url="http://127.0.0.1")


def test_console_page_and_assets_with_strict_headers(harness, tmp_path, monkeypatch):
    built = tmp_path / "web_dist"
    (built / "assets").mkdir(parents=True)
    (built / "index.html").write_text('<!doctype html><script type="module" '
                                      'src="/assets/app.js"></script>', encoding="utf-8")
    (built / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(app_module, "CONSOLE_DIR", built)
    with client_for(harness) as client:
        page = client.get("/")
        assert page.status_code == 200 and "assets/app.js" in page.text
        policy = page.headers["content-security-policy"]
        assert "script-src 'self'" in policy and "frame-ancestors 'none'" in policy
        assert "unsafe-inline" not in policy
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["cache-control"] == "no-store"
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200 and "no-store" not in asset.headers.get(
            "cache-control", "")
        assert client.get("/api/v1/state").headers["x-content-type-options"] == "nosniff"


def test_console_not_built_says_how(harness, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "CONSOLE_DIR", tmp_path / "missing")
    with client_for(harness) as client:
        page = client.get("/")
        assert page.status_code == 503 and "npm run build" in page.text


def test_real_console_build_has_no_inline_code():
    """The built console must work under the CSP: no inline scripts or styles."""
    index = app_module.CONSOLE_DIR / "index.html"
    if not index.exists():
        pytest.skip("console not built")
    html = index.read_text(encoding="utf-8")
    assert "<script>" not in html and "style=" not in html and "<style" not in html
    assert '<script type="module" crossorigin src="/assets/' in html


def test_settings_view_has_no_secrets(harness):
    harness.settings.peakai.vendor_code = "mahadev"
    with client_for(harness) as client:
        body = client.get("/api/v1/settings", headers=HEADERS).json()
        assert body["peakai"]["vendor_code"] == "mahadev"
        assert body["upload"]["max_workers"] == harness.settings.upload.max_workers
        text = json.dumps(body)
        assert "key_file" not in text and "token" not in text and "secrets" not in text
        assert client.get("/api/v1/settings").status_code == 401
    with client_for(harness, settings=False) as client:
        assert client.get("/api/v1/settings", headers=HEADERS).status_code == 503


def test_pack_check_apply_and_cancel_over_http(harness, tmp_path):
    good = make_pack(tmp_path / "pack.zip").read_bytes()
    pack_dir = harness.settings.resolve(harness.settings.peakai.pack_dir)
    key_file = harness.settings.resolve(harness.settings.peakai.key_file)
    upload = pack_dir / "upload.zip"
    with client_for(harness) as client:
        assert client.post("/api/v1/pack/apply", headers=HEADERS).status_code == 409
        checked = client.post("/api/v1/pack/check?offline=true", headers=HEADERS, content=good)
        body = checked.json()
        assert body["ok"] is True and body["pack"]["bucket"] == "peak-lake-test"
        assert KEY not in checked.text and upload.exists()
        assert any("vendor_code is not set" in warning for warning in body["warnings"])
        cancelled = client.post("/api/v1/pack/cancel", headers=HEADERS)
        assert cancelled.status_code == 200 and not upload.exists()

        client.post("/api/v1/pack/check?offline=true", headers=HEADERS, content=good)
        applied = client.post("/api/v1/pack/apply", headers=HEADERS).json()
        assert applied == {"applied": "upload.zip", "restart_needed": True}
        assert not upload.exists()  # The upload held the key: it is gone.
        assert key_file.read_text().strip() == KEY

        bad = make_pack(tmp_path / "bad.zip", **{"laptop_key.txt": None}).read_bytes()
        refused = client.post("/api/v1/pack/check?offline=true", headers=HEADERS,
                              content=bad).json()
        assert refused["ok"] is False and not upload.exists()
        too_big = client.post("/api/v1/pack/check", headers=HEADERS,
                              content=b"x" * (5 * 1024 * 1024 + 1))
        assert too_big.status_code == 400
        assert client.post("/api/v1/pack/check", content=good).status_code == 401


def test_console_sign_in_link(tmp_path, capsys):
    config = tmp_path / "config.yaml"
    config.write_text("api:\n  port: 18999\n", encoding="utf-8")
    assert main(["--config", str(config), "console", "--print-link"]) == 1
    assert "Cannot read the station's API token" in capsys.readouterr().out
    token_file = tmp_path / "data" / "station" / "api-token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("abc123", encoding="utf-8")
    assert main(["--config", str(config), "console", "--print-link"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:18999/#token=abc123"
