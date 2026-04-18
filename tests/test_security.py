"""
Tests for security controls — headers, upload protection, input validation, token encryption.
"""
import pytest


def test_security_headers_present(auth_client):
    r = auth_client.get("/", follow_redirects=True)
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in r.headers
    assert "Strict-Transport-Security" in r.headers
    assert "Referrer-Policy" in r.headers


def test_csp_blocks_framing(auth_client):
    r = auth_client.get("/", follow_redirects=True)
    csp = r.headers.get("Content-Security-Policy", "")
    assert "frame-ancestors 'none'" in csp


def test_settings_token_encryption_roundtrip(app, db):
    """Fortnox tokens must be stored encrypted and decrypted transparently."""
    pytest.importorskip("cryptography", reason="cryptography package not installed")
    from models import Settings
    with app.app_context():
        Settings.set("fortnox_access_token", "my-secret-token")
        row = Settings.query.filter_by(key="fortnox_access_token").first()
        # Raw DB value must NOT be the plaintext token
        assert row.value != "my-secret-token"
        # But get() must return the original value
        assert Settings.get("fortnox_access_token") == "my-secret-token"


def test_settings_non_token_not_encrypted(app, db):
    """Non-token settings should be stored as plaintext."""
    from models import Settings
    with app.app_context():
        Settings.set("some_other_setting", "plainvalue")
        row = Settings.query.filter_by(key="some_other_setting").first()
        assert row.value == "plainvalue"


def test_upload_path_traversal_blocked(auth_client):
    # Werkzeug normalises ../ before routing, so we test with a crafted name
    # that stays within the route but tries to escape the upload folder.
    # The realpath guard in uploaded_file() should return 404.
    r = auth_client.get("/uploads/..%2Fconfig.py")
    assert r.status_code in (404, 400)


def test_upload_requires_login(client):
    r = client.get("/uploads/test.jpg")
    assert r.status_code in (302, 401)


def test_csrf_meta_tag_present(auth_client):
    r = auth_client.get("/", follow_redirects=True)
    assert b'name="csrf-token"' in r.data


def test_invalid_float_input_does_not_crash(auth_client, db, sample_client):
    r = auth_client.post(f"/clients/{sample_client.id}/edit", data={
        "name": "Test",
        "hourly_rate": "notanumber",
        "km_rate": "25",
        "payment_days": "30",
        "invoice_language": "sv",
    }, follow_redirects=True)
    assert r.status_code == 200  # should handle gracefully, not crash


def test_invalid_date_in_mileage_does_not_crash(auth_client, db, sample_project):
    r = auth_client.post("/mileage", data={
        "project_id": sample_project.id,
        "entry_date": "not-a-date",
        "km": "100",
        "billable": "1",
    }, follow_redirects=True)
    assert r.status_code == 200  # should handle gracefully, not crash
