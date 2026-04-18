"""
Shared fixtures for the onedesk test suite.
"""
import pytest
from datetime import date
from werkzeug.security import generate_password_hash
from sqlalchemy.pool import StaticPool

from app import app as flask_app, init_db, limiter as _limiter
from models import db as _db, User, Client, Project, PurchaseOrder, POHourType, TimeEntry, Invoice


@pytest.fixture(scope="session")
def app():
    flask_app.config.update(
        TESTING=True,
        # StaticPool shares one in-memory DB across all connections in the same process
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_ENGINE_OPTIONS={
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        },
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test-secret",
        ADMIN_PASSWORD="testpass",
        UPLOAD_FOLDER="/tmp/onedesk_test_uploads",
        # Disable rate limiting so login calls don't hit 429 during tests
        RATELIMIT_ENABLED=False,
    )
    with flask_app.app_context():
        init_db()
    yield flask_app


@pytest.fixture(autouse=True)
def reset_rate_limits(app):
    """Reset rate limit counters before every test so login never hits 429."""
    with app.app_context():
        _limiter.reset()
    yield


@pytest.fixture
def db(app):
    """Provide a clean database for each test."""
    with app.app_context():
        _db.drop_all()
        _db.create_all()
        # Create admin user
        user = User(username="admin", password_hash=generate_password_hash("testpass"))
        _db.session.add(user)
        _db.session.commit()
        yield _db
        _db.session.remove()


@pytest.fixture
def client(app, db):
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth_client(app, db):
    """Test client that is already logged in."""
    with app.test_client() as c:
        c.post("/login", data={"password": "testpass"})
        yield c


# ── Data factory fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def sample_client(db):
    c = Client(
        name="Test Client AB",
        contact_email="client@example.com",
        hourly_rate=1500.0,
        km_rate=25.0,
        payment_days=30,
    )
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture
def sample_project(db, sample_client):
    p = Project(
        client_id=sample_client.id,
        name="Test Assignment",
        project_number="P2601",
        active=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def sample_po(db, sample_project):
    po = PurchaseOrder(
        project_id=sample_project.id,
        po_number="PO-001",
        hourly_rate=1500.0,
        km_rate=30.0,
        valid_from=date(2026, 1, 1),
        valid_to=date(2026, 12, 31),
    )
    db.session.add(po)
    db.session.flush()
    ht = POHourType(po_id=po.id, name="Normal", billable=True, sort_order=0, hourly_rate=1500.0)
    db.session.add(ht)
    db.session.commit()
    return po
