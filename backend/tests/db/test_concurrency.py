"""Concurrent finalize, with two real database connections.

Optimistic locking is only meaningful under actual concurrency: a single-
connection test proves the version check reads the right column, not that two
simultaneous writers are serialised. These open two sessions and race them.
"""
from __future__ import annotations

import json
import threading

import psycopg
import pytest

from .conftest import AUTH_A, CLINIC_A, PATIENT_A
from .test_finalize_visit import NOTE, SQL

pytestmark = pytest.mark.db


def _session(dsn: str):
    conn = psycopg.connect(dsn, autocommit=False)
    conn.execute("set role authenticated")
    conn.execute("select set_config('request.jwt.claims', %s, false)",
                 (f'{{"sub":"{AUTH_A}","role":"authenticated"}}',))
    return conn


def _finalize(conn, *, visit_id=None, expected_version=None, draft=False, attested=True):
    return conn.execute(SQL, (
        PATIENT_A, json.dumps(NOTE), json.dumps([{"fact_type": "symptom", "value": "cough"}]),
        visit_id, expected_version, draft, attested, True, "verbal",
    )).fetchone()[0]


@pytest.fixture
def two_sessions(committing_database):
    """Two independent connections, cleaned up whatever happens."""
    a, b = _session(committing_database), _session(committing_database)
    yield a, b
    for conn in (a, b):
        try:
            conn.rollback()
        finally:
            conn.close()


@pytest.fixture
def committed_draft(committing_database):
    """A draft that really exists on disk, so both sessions can see it.

    Nothing is cleaned up afterwards: this database is private to the module,
    and deleting audit rows to tidy up would break the very hash chain the
    other tests verify.
    """
    conn = _session(committing_database)
    draft = _finalize(conn, draft=True, attested=False)
    conn.commit()
    conn.close()
    return draft


def test_two_simultaneous_finalizes_do_not_both_succeed(two_sessions, committed_draft):
    """Last-write-wins on a signed clinical record is the failure this prevents.

    Both sessions read the same version, then both try to sign. `select ... for
    update` inside finalize_visit() serialises them: the second blocks until the
    first commits, then finds the visit already signed and is refused.
    """
    a, b = two_sessions
    visit_id, version = committed_draft["visit_id"], committed_draft["version"]

    first = _finalize(a, visit_id=visit_id, expected_version=version)
    assert first["status"] == "approved"

    outcome: dict = {}
    barrier = threading.Event()

    def second_writer():
        try:
            _finalize(b, visit_id=visit_id, expected_version=version)
            outcome["result"] = "succeeded"
        except psycopg.Error as e:
            outcome["error"] = e
        finally:
            barrier.set()

    thread = threading.Thread(target=second_writer, daemon=True)
    thread.start()
    # The second writer is now blocked on the row lock. Releasing it must not
    # let it overwrite the signed note.
    a.commit()
    barrier.wait(timeout=10)
    thread.join(timeout=10)

    assert "result" not in outcome, "a second finalize must not succeed on a signed visit"
    error = outcome["error"]
    assert isinstance(error, psycopg.errors.CheckViolation | psycopg.errors.SerializationFailure)


def test_the_signed_note_is_the_first_writers(two_sessions, committed_draft):
    a, b = two_sessions
    visit_id = committed_draft["visit_id"]

    a.execute(SQL, (
        PATIENT_A, json.dumps({**NOTE, "assessment": "First writer's assessment"}),
        json.dumps([]), visit_id, committed_draft["version"], False, True, True, "verbal",
    ))
    a.commit()

    try:
        b.execute(SQL, (
            PATIENT_A, json.dumps({**NOTE, "assessment": "Second writer's assessment"}),
            json.dumps([]), visit_id, committed_draft["version"], False, True, True, "verbal",
        ))
        b.commit()
    except psycopg.Error:
        b.rollback()

    a.execute("set role authenticated")
    a.execute("select set_config('request.jwt.claims', %s, false)",
              (f'{{"sub":"{AUTH_A}","role":"authenticated"}}',))
    assessment = a.execute(
        "select assessment from public.soap_notes where visit_id = %s", (visit_id,)).fetchone()[0]
    assert assessment == "First writer's assessment"
    a.commit()


def test_concurrent_audit_writes_produce_a_single_valid_chain(two_sessions):
    """The chain trigger takes a per-clinic advisory lock. Without it two
    concurrent inserts can read the same predecessor and fork the chain."""
    a, b = two_sessions

    a.execute("select public.write_audit('save_draft', 'visit', null)")
    b_done = threading.Event()

    def writer_b():
        try:
            b.execute("select public.write_audit('delete_visit', 'visit', null)")
            b.commit()
        finally:
            b_done.set()

    thread = threading.Thread(target=writer_b, daemon=True)
    thread.start()
    a.commit()
    b_done.wait(timeout=10)
    thread.join(timeout=10)

    checker = psycopg.connect(_dsn_of(a), autocommit=True)
    try:
        breaks = checker.execute(
            "select seq, problem from public.verify_audit_chain(%s)", (CLINIC_A,)).fetchall()
        assert breaks == [], f"concurrent writes forked the chain: {breaks}"
    finally:
        checker.close()


def _dsn_of(conn) -> str:
    info = conn.info
    return f"postgresql://{info.user}@{info.host}:{info.port}/{info.dbname}"


