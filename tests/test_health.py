"""Tests for the /healthz liveness/readiness probe."""


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert "build_time" in data


def test_healthz_is_public(client):
    """The probe must be reachable without authentication (no login redirect)."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
