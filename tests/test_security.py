"""
Tests for security controls — headers, upload protection, input validation.
"""


def test_security_headers_present(auth_client):
    r = auth_client.get("/", follow_redirects=True)
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in r.headers


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
