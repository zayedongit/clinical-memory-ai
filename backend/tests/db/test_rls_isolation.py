"""Multi-tenant isolation, proven against the database.

The claim this project makes is that a clinic cannot reach another clinic's
data *even if the application code asks for it*. That claim is only worth
something if it is tested by asking for it, so every test here is an attempt
to cross the boundary.
"""
from __future__ import annotations

import psycopg
import pytest

from .conftest import CLINIC_A, CLINIC_B, PATIENT_A, PATIENT_B

pytestmark = pytest.mark.db

VISIT_B = "cccccccc-0000-0000-0000-00000000000b"


# --------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------- #
def test_each_clinic_sees_only_its_own_patients(doctor_a, doctor_b):
    assert [r[0] for r in doctor_a.all("select name from public.patients")] == \
        ["Asha Rao (Clinic A)"]
    assert [r[0] for r in doctor_b.all("select name from public.patients")] == \
        ["Bilal Khan (Clinic B)"]


def test_asking_for_another_clinics_patient_by_id_returns_nothing(doctor_a):
    """Not an error — the row simply does not exist for this caller.

    This is the property that makes a forgotten `where clinic_id = ...` a
    non-event instead of a breach.
    """
    assert doctor_a.all("select id from public.patients where id = %s", (PATIENT_B,)) == []


def test_an_explicit_cross_clinic_filter_still_returns_nothing(doctor_a):
    assert doctor_a.all("select id from public.patients where clinic_id = %s", (CLINIC_B,)) == []


def test_a_clinic_cannot_see_another_clinics_users(doctor_a):
    assert {str(r[0]) for r in doctor_a.all("select clinic_id from public.users")} == {CLINIC_A}


def test_a_clinic_cannot_see_another_clinics_clinic_row(doctor_a):
    assert [str(r[0]) for r in doctor_a.all("select id from public.clinics")] == [CLINIC_A]


def test_an_unauthenticated_session_sees_nothing(db):
    db.execute("set local role anon")
    assert db.execute("select count(*) from public.patients").fetchone()[0] == 0
    db.execute("reset role")


def test_a_token_for_an_unknown_user_sees_nothing(db):
    """current_clinic_id() returns NULL, and `clinic_id = NULL` is never true."""
    db.execute("set local role authenticated")
    db.execute("select set_config('request.jwt.claims', %s, true)",
               ('{"sub":"00000000-0000-0000-0000-0000000000ff","role":"authenticated"}',))
    assert db.execute("select count(*) from public.patients").fetchone()[0] == 0
    db.execute("reset role")


# --------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------- #
def test_inserting_a_patient_into_another_clinic_is_refused(doctor_a):
    error = doctor_a.expect_error(
        "insert into public.patients (clinic_id, name) values (%s, 'Smuggled')", (CLINIC_B,))
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)


def test_updating_another_clinics_patient_affects_nothing(doctor_a, service_role):
    doctor_a.run("update public.patients set name = 'Hacked' where id = %s", (PATIENT_B,))
    assert service_role.one("select name from public.patients where id = %s", (PATIENT_B,)) == \
        "Bilal Khan (Clinic B)"


def test_a_patient_cannot_be_moved_to_another_clinic(doctor_a):
    """The WITH CHECK clause is what stops an update walking a row out of the
    tenant it belongs to — USING alone would permit it."""
    error = doctor_a.expect_error(
        "update public.patients set clinic_id = %s where id = %s", (CLINIC_B, PATIENT_A))
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)


@pytest.mark.parametrize("table", ["patients", "visits", "soap_notes", "clinical_facts"])
def test_hard_deleting_clinical_records_is_refused(doctor_a, table):
    """Clinical records are medico-legal documents: retained and hidden, never
    destroyed. The privilege is revoked outright rather than left to policy."""
    error = doctor_a.expect_error(f"delete from public.{table}")
    assert error is not None, f"DELETE on {table} was permitted"
    assert isinstance(error, psycopg.errors.InsufficientPrivilege | psycopg.errors.RaiseException)


def test_visits_and_facts_are_clinic_scoped(doctor_a, service_role):
    """Create clinic B state with the RLS-bypassing role, then confirm clinic A
    cannot reach it."""
    service_role.run(
        "insert into public.visits (id, patient_id, clinic_id, status) values (%s, %s, %s, 'approved')",
        (VISIT_B, PATIENT_B, CLINIC_B),
    )
    service_role.run(
        "insert into public.clinical_facts "
        "(patient_id, clinic_id, visit_id, fact_type, value, source) "
        "values (%s, %s, %s, 'diagnosis', 'Secret condition', 'doctor_entered')",
        (PATIENT_B, CLINIC_B, VISIT_B),
    )

    assert doctor_a.one("select count(*) from public.visits") == 0
    assert doctor_a.one(
        "select count(*) from public.clinical_facts where value = 'Secret condition'") == 0


def test_a_doctor_cannot_read_another_clinics_audit_trail(doctor_a, service_role):
    service_role.run(
        "insert into public.audit_log (clinic_id, action, entity) values (%s, 'sign_visit', 'visit')",
        (CLINIC_B,),
    )
    assert doctor_a.one("select count(*) from public.audit_log") == 0


# --------------------------------------------------------------------- #
# Structural guarantees — a new table must not ship without RLS
# --------------------------------------------------------------------- #
CLINIC_SCOPED_TABLES = ("clinics", "users", "patients", "visits", "soap_notes",
                        "clinical_facts", "audit_log")


@pytest.mark.parametrize("table", CLINIC_SCOPED_TABLES)
def test_row_level_security_is_enabled_on_every_clinical_table(db, table):
    enabled = db.execute(
        "select relrowsecurity from pg_class where oid = %s::regclass", (f"public.{table}",)
    ).fetchone()[0]
    assert enabled is True, f"{table} has RLS disabled"


def test_no_public_table_is_left_without_row_level_security(db):
    """Catches the table nobody remembered to add to the list above.

    The knowledge-base tables are global read-only reference data and are
    allowed a `using (true)` read policy; everything else must be scoped.
    """
    unprotected = db.execute(
        """
        select c.relname from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity
        """
    ).fetchall()
    assert unprotected == [], f"tables without RLS: {[r[0] for r in unprotected]}"


@pytest.mark.parametrize("table", ("patients", "visits", "soap_notes", "clinical_facts"))
def test_every_writable_clinical_table_has_select_insert_and_update_policies(db, table):
    """The bug that started this work: soap_notes had SELECT/INSERT/DELETE
    policies but no UPDATE policy, so PostgREST reported success for updates
    that matched zero rows — silent clinical data loss on every draft finalize.
    """
    commands = {r[0] for r in db.execute(
        "select cmd from pg_policies where schemaname = 'public' and tablename = %s", (table,)
    ).fetchall()}
    for required in ("SELECT", "INSERT", "UPDATE"):
        assert required in commands, f"{table} has no {required} policy"


def test_audit_log_has_no_insert_policy_for_ordinary_users(db):
    """The log must not be forgeable from an ordinary session; writes go through
    a SECURITY DEFINER wrapper that stamps the actor from the caller's JWT."""
    commands = {r[0] for r in db.execute(
        "select cmd from pg_policies where schemaname = 'public' and tablename = 'audit_log'"
    ).fetchall()}
    assert "INSERT" not in commands


def test_every_clinical_policy_scopes_by_clinic(db):
    """`using (true)` on a clinic-scoped table disables isolation while still
    looking like RLS is switched on."""
    rows = db.execute(
        "select tablename, policyname, qual, with_check from pg_policies "
        "where schemaname = 'public' and tablename = any(%s)",
        (list(CLINIC_SCOPED_TABLES),),
    ).fetchall()
    assert rows, "expected policies to exist"
    for tablename, policyname, qual, with_check in rows:
        for clause in (qual, with_check):
            if clause is None:
                continue
            assert clause.strip().lower() != "true", \
                f"{tablename}.{policyname} is unconditionally permissive"
            assert "current_clinic_id" in clause, \
                f"{tablename}.{policyname} does not scope by clinic: {clause}"


def test_current_clinic_id_only_ever_returns_the_callers_own_clinic(doctor_a, doctor_b):
    """The helper is SECURITY DEFINER, so it is worth proving it cannot be
    coaxed into returning someone else's clinic."""
    assert str(doctor_a.one("select public.current_clinic_id()")) == CLINIC_A
    assert str(doctor_b.one("select public.current_clinic_id()")) == CLINIC_B
