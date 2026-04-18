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


def test_ad_domain_whitelist_blocks_foreign_domain(app, client):
    """AD auto-login must reject email domains not in ALLOWED_EMAIL_DOMAINS."""
    import base64, json
    app.config["ALLOWED_EMAIL_DOMAINS"] = "allowed.com"
    principal = {
        "claims": [
            {"typ": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
             "val": "user@notallowed.com"},
            {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier",
             "val": "some-oid"},
        ]
    }
    header = base64.b64encode(json.dumps(principal).encode()).decode()
    r = client.get("/", headers={"X-MS-CLIENT-PRINCIPAL": header})
    assert r.status_code == 403
    app.config["ALLOWED_EMAIL_DOMAINS"] = ""  # restore


def test_ad_domain_whitelist_allows_permitted_domain(app, db, client):
    """AD auto-login must allow emails from whitelisted domains."""
    import base64, json
    from models import User
    from werkzeug.security import generate_password_hash
    with app.app_context():
        u = User(username="aduser", email="user@allowed.com",
                 ad_oid="test-oid-123", active=True,
                 password_hash=generate_password_hash("x"))
        db.session.add(u)
        db.session.commit()

    app.config["ALLOWED_EMAIL_DOMAINS"] = "allowed.com"
    principal = {
        "claims": [
            {"typ": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
             "val": "user@allowed.com"},
            {"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier",
             "val": "test-oid-123"},
        ]
    }
    header = base64.b64encode(json.dumps(principal).encode()).decode()
    r = client.get("/", headers={"X-MS-CLIENT-PRINCIPAL": header}, follow_redirects=True)
    assert r.status_code == 200
    app.config["ALLOWED_EMAIL_DOMAINS"] = ""  # restore
