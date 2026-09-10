"""Database tests against a real PostgreSQL running the real migrations.

Why these exist rather than mocks: tenant isolation, attestation, optimistic
locking and the append-only audit trail are all enforced *in the database*.
Testing them against a fake would test the fake. These run the actual
migrations and then try to break the rules the way an attacker or a bug would.

To run them, point `CMA_TEST_DATABASE_URL` at a scratch PostgreSQL:

    export CMA_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres
    uv run pytest tests/db

They are skipped (not failed) when that variable is absent, so the unit suite
still runs on a laptop with no database. CI always sets it, so the skip is
never load-bearing.
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((REPO / "supabase" / "migrations").glob("*.sql"))
AUTH_SHIM = REPO / "supabase" / "tests" / "00_auth_shim.sql"

DSN = os.getenv("CMA_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DSN, reason="CMA_TEST_DATABASE_URL is not set; database tests skipped"
)

# Fixed ids so failures are readable.
CLINIC_A = "11111111-1111-1111-1111-111111111111"
CLINIC_B = "22222222-2222-2222-2222-222222222222"
DOCTOR_A = "99999999-0000-0000-0000-00000000000a"
DOCTOR_B = "99999999-0000-0000-0000-00000000000b"
STAFF_A = "99999999-0000-0000-0000-00000000000c"
AUTH_A = "00000000-0000-0000-0000-0000000000aa"
AUTH_B = "00000000-0000-0000-0000-0000000000bb"
AUTH_STAFF = "00000000-0000-0000-0000-0000000000cc"
PATIENT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PATIENT_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@contextmanager
def build_database():
    """Build a throwaway database from the committed migrations.

    Applying every migration from scratch is itself the test that the
    migrations reproduce the running system — the defect that started this
    work was a schema that only existed on the live Supabase project.
    """
    db_name = f"cma_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'create database "{db_name}"')

    target = _swap_database(DSN, db_name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute(AUTH_SHIM.read_text())
            for path in MIGRATIONS:
                try:
                    conn.execute(path.read_text())
                except Exception as e:  # pragma: no cover - surfaced as a test error
                    raise AssertionError(f"migration {path.name} failed: {e}") from e
            _seed(conn)
        yield target
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'drop database if exists "{db_name}" with (force)')


@pytest.fixture(scope="session")
def migrated_database():
    """Shared by the transactional tests, which never commit."""
    with build_database() as dsn:
        yield dsn


@pytest.fixture(scope="module")
def committing_database():
    """A private database for tests that must genuinely commit.

    Concurrency cannot be tested inside a rolled-back transaction — two
    sessions have to see each other's committed writes — so those tests get
    their own database rather than leaving residue in the shared one.
    """
    with build_database() as dsn:
        yield dsn


def _swap_database(dsn: str, name: str) -> str:
    head, _, _ = dsn.rpartition("/")
    return f"{head}/{name}"


def _seed(conn) -> None:
    """Two clinics, two doctors, one staff member, one patient each."""
    conn.execute(
        """
        insert into public.users (id, clinic_id, auth_uid, name, role) values
          (%s, %s, %s, 'Dr A', 'doctor'),
          (%s, %s, %s, 'Dr B', 'doctor'),
          (%s, %s, %s, 'Front desk A', 'staff')
        on conflict (id) do nothing
        """,
        (DOCTOR_A, CLINIC_A, AUTH_A, DOCTOR_B, CLINIC_B, AUTH_B, STAFF_A, CLINIC_A, AUTH_STAFF),
    )


@pytest.fixture
def db(migrated_database):
    """A connection wrapped in a transaction that is always rolled back.

    Every test therefore sees the same seeded state and nothing leaks between
    them, which matters more than usual here because several tests deliberately
    corrupt data to prove a trigger catches it.
    """
    conn = psycopg.connect(migrated_database, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


class Session:
    """Runs statements as one authenticated user, exactly as PostgREST does:
    `set local role authenticated` plus the `request.jwt.claims` GUC.

    Statements run inside a SAVEPOINT so a test can assert that a write is
    refused and then keep using the same connection — without one, the first
    expected error aborts the whole transaction.
    """

    def __init__(self, conn, auth_uid: str, role: str = "authenticated") -> None:
        self.conn = conn
        self.auth_uid = auth_uid
        self.role = role
        self._n = 0

    def activate(self) -> Session:
        self.conn.execute(f"set local role {self.role}")
        self.conn.execute(
            "select set_config('request.jwt.claims', %s, true)",
            (f'{{"sub":"{self.auth_uid}","role":"authenticated"}}',),
        )
        return self

    def __enter__(self) -> Session:
        return self.activate()

    def __exit__(self, *exc) -> None:
        self.conn.execute("reset role")

    def _savepoint(self) -> str:
        self._n += 1
        return f"sp_{id(self) % 100000}_{self._n}"

    def all(self, sql: str, params: tuple = ()) -> list[tuple]:
        self.activate()
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()):
        rows = self.all(sql, params)
        return rows[0][0] if rows else None

    def run(self, sql: str, params: tuple = ()) -> None:
        self.activate()
        self.conn.execute(sql, params)

    def expect_error(self, sql: str, params: tuple = ()):
        """Run a statement expected to fail, contained in a savepoint."""
        self.activate()
        name = self._savepoint()
        self.conn.execute(f"savepoint {name}")
        try:
            self.conn.execute(sql, params)
        except psycopg.Error as e:
            self.conn.execute(f"rollback to savepoint {name}")
            return e
        self.conn.execute(f"release savepoint {name}")
        return None


@pytest.fixture
def doctor_a(db):
    return Session(db, AUTH_A).activate()


@pytest.fixture
def doctor_b(db):
    return Session(db, AUTH_B)


@pytest.fixture
def staff_a(db):
    return Session(db, AUTH_STAFF)


@pytest.fixture
def service_role(db):
    """The service_role key bypasses RLS. Used to set up cross-tenant state and
    to prove that even this role cannot rewrite the audit log."""
    return Session(db, AUTH_A, role="service_role")
