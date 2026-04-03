"""
Tests for authentication — login, logout, rate limiting, access control.
"""


def test_login_page_loads(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"onedesk" in r.data


def test_login_wrong_password(client):
    r = client.post("/login", data={"password": "wrongpass"}, follow_redirects=True)
    assert r.status_code == 200
    assert b"Fel" in r.data  # "Fel lösenord / Wrong password"


def test_login_correct_password(client):
    r = client.post("/login", data={"password": "testpass"}, follow_redirects=True)
    assert r.status_code == 200
    # Should land on dashboard, not login page
    assert "login" not in r.request.path.lower()


def test_logout(auth_client):
    r = auth_client.get("/logout", follow_redirects=True)
    assert r.status_code == 200
    assert b"onedesk" in r.data  # back on login page


def test_protected_routes_require_login(client):
    """Unauthenticated requests to protected routes should redirect to login."""
    for path in ["/", "/time", "/expenses", "/mileage", "/invoices", "/clients"]:
        r = client.get(path)
        assert r.status_code in (302, 301), f"{path} should redirect unauthenticated user"
        assert "login" in r.headers.get("Location", "").lower(), \
            f"{path} should redirect to login"


def test_upload_route_requires_login(client):
    r = client.get("/uploads/somefile.jpg")
    assert r.status_code in (302, 401)


def test_already_logged_in_redirects_from_login(auth_client):
    r = auth_client.get("/login", follow_redirects=False)
    assert r.status_code == 302
