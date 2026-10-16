"""
Upgrading a pre-internal-codes database.

Two things have to happen on an existing SQLite file:
  - time_entry.project_id must lose its NOT NULL, which SQLite can only do by
    rebuilding the table
  - non-billable time logged as a time_type string must move onto the matching
    internal code and let go of the client assignment it was parked on

Runs against a real file database, because the in-memory fixture is built from
the current models and so never exercises either path.
"""
import sqlite3
import subprocess
import sys
import textwrap

import pytest


LEGACY_SCHEMA = """
CREATE TABLE client (id INTEGER PRIMARY KEY, name TEXT, hourly_rate REAL DEFAULT 1500,
  km_rate REAL DEFAULT 25, active BOOLEAN DEFAULT 1, currency TEXT DEFAULT 'SEK',
  payment_days INTEGER DEFAULT 30, invoice_language TEXT DEFAULT 'sv');
CREATE TABLE project (id INTEGER PRIMARY KEY, client_id INTEGER, name TEXT, active BOOLEAN DEFAULT 1);
CREATE TABLE po_hour_type (id INTEGER PRIMARY KEY, po_id INTEGER, name TEXT);
CREATE TABLE invoice (id INTEGER PRIMARY KEY);
CREATE TABLE time_entry (
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, entry_date DATE,
  hours REAL, time_type VARCHAR(50) DEFAULT 'Normal', hour_type_id INTEGER,
  description TEXT, invoiced BOOLEAN DEFAULT 0, invoice_id INTEGER, created_at DATETIME);
CREATE INDEX ix_time_entry_date ON time_entry(entry_date);
INSERT INTO client (id, name) VALUES (1, 'Acme AB');
INSERT INTO project (id, client_id, name) VALUES (1, 1, 'Migration');
INSERT INTO time_entry (project_id, entry_date, hours, time_type, description, invoiced) VALUES
  (1, '2026-02-02', 8, 'Sjuk',     'influensa',    0),
  (1, '2026-02-03', 8, 'Semester', NULL,           0),
  (1, '2026-02-04', 8, 'VAB',      NULL,           0),
  (1, '2026-02-05', 8, 'Normal',   'riktigt jobb', 0),
  (1, '2026-02-06', 8, 'Sjuk',     NULL,           1);
"""

RUNNER = textwrap.dedent("""
    import sys
    from app import init_db
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "twice":
        init_db()
""")


@pytest.fixture
def migrated_db(tmp_path, request):
    """A legacy database with init_db() run over it. Param 'twice' runs it again."""
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.executescript(LEGACY_SCHEMA)
    con.commit()
    con.close()

    runner = tmp_path / "run_migration.py"
    runner.write_text(RUNNER)

    times = getattr(request, "param", "once")
    proc = subprocess.run(
        [sys.executable, str(runner), times],
        capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parent.parent),
            "DATABASE_URL": f"sqlite:///{path}",
            "SECRET_KEY": "migration-test",
        },
    )
    assert proc.returncode == 0, f"init_db failed:\n{proc.stdout}\n{proc.stderr}"
    return sqlite3.connect(path)


def test_project_id_becomes_nullable(migrated_db):
    cols = {c[1]: c[3] for c in migrated_db.execute("PRAGMA table_info(time_entry)")}
    assert cols["project_id"] == 0          # 0 = nullable
    assert "internal_code_id" in cols


def test_rebuild_preserves_rows_and_indexes(migrated_db):
    assert migrated_db.execute("SELECT COUNT(*) FROM time_entry").fetchone()[0] == 5
    # The description column survived the copy
    assert migrated_db.execute(
        "SELECT description FROM time_entry WHERE id=1").fetchone()[0] == "influensa"

    indexes = {r[0] for r in migrated_db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='time_entry'"
        " AND name NOT LIKE 'sqlite_%'")}
    assert "ix_time_entry_date" in indexes           # pre-existing index restored
    assert "ix_time_entry_internal_code" in indexes  # new one added
    # The scratch table is gone
    assert not migrated_db.execute(
        "SELECT name FROM sqlite_master WHERE name='time_entry__new'").fetchall()


def test_legacy_absence_types_move_onto_codes(migrated_db):
    rows = dict(migrated_db.execute("""
        SELECT t.time_type, c.code FROM time_entry t
        JOIN internal_code c ON c.id = t.internal_code_id"""))
    assert rows == {"Sjuk": "SJUK", "Semester": "SEM", "VAB": "VAB"}

    # ...and let go of the client assignment they were parked on
    orphaned = migrated_db.execute(
        "SELECT COUNT(*) FROM time_entry WHERE internal_code_id IS NOT NULL"
        " AND project_id IS NOT NULL").fetchone()[0]
    assert orphaned == 0


def test_billable_and_invoiced_rows_are_untouched(migrated_db):
    normal = migrated_db.execute(
        "SELECT project_id, internal_code_id FROM time_entry WHERE time_type='Normal'").fetchone()
    assert normal == (1, None)

    # An already-invoiced absence row stays on its assignment: rewriting it would
    # change an invoice after the fact.
    invoiced = migrated_db.execute(
        "SELECT project_id, internal_code_id FROM time_entry WHERE invoiced=1").fetchone()
    assert invoiced == (1, None)


def test_default_codes_are_seeded(migrated_db):
    codes = {r[0] for r in migrated_db.execute("SELECT code FROM internal_code")}
    assert {"SALES", "SJUK", "SEM", "VAB", "ADMIN", "UTB"} <= codes


@pytest.mark.parametrize("migrated_db", ["twice"], indirect=True)
def test_migration_is_idempotent(migrated_db):
    assert migrated_db.execute("SELECT COUNT(*) FROM internal_code").fetchone()[0] == 6
    assert migrated_db.execute("SELECT COUNT(*) FROM time_entry").fetchone()[0] == 5
    assert migrated_db.execute(
        "SELECT COUNT(*) FROM time_entry WHERE internal_code_id IS NOT NULL").fetchone()[0] == 3
