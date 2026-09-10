"""PostgREST filter construction.

These test the fix for a real injection: the patient search interpolated the
raw query into a filter string, so a `)` or `,` in the input closed the filter
group early and the remainder was parsed as further filter clauses.
"""
from __future__ import annotations

import pytest

from app.core import pgrst


def test_plain_value_is_quoted():
    assert pgrst.quote("Sharma") == '"Sharma"'


def parse_or_group(built: str) -> list[tuple[str, str]]:
    """Parse `(col.op."value",col.op."value")` the way PostgREST does.

    Written as a real parser rather than a substring check so the assertions
    below prove the *structure* is intact — a payload that merely contains
    `.ilike.` should not be able to make a naive check fail or pass.
    """
    assert built.startswith("(") and built.endswith(")")
    body, i, clauses = built[1:-1], 0, []
    while i < len(body):
        head_end = body.index('."', i)
        column, op = body[i:head_end].split(".", 1)
        i = head_end + 2
        value, escaped = [], False
        while True:
            ch = body[i]
            i += 1
            if escaped:
                value.append(ch)
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                break
            else:
                value.append(ch)
        clauses.append((f"{column}.{op}", "".join(value)))
        if i < len(body):
            assert body[i] == ",", f"clauses must be comma separated, found {body[i]!r}"
            i += 1
    return clauses


@pytest.mark.parametrize("payload", [
    # Each of these previously escaped the `or=(...)` group.
    "a),name.eq.x,(",
    "x,phone.ilike.*9*",
    ")",
    "'; drop table patients; --",
    'quote"inside',
    "back\\slash",
    '"),*',
])
def test_injection_payloads_stay_inside_one_quoted_value(payload):
    clauses = parse_or_group(pgrst.or_ilike(payload, "name", "phone"))

    # Exactly the two clauses we asked for, no more — the payload did not
    # become a filter clause of its own.
    assert [c for c, _ in clauses] == ["name.ilike", "phone.ilike"]
    # And each clause's value is the payload, wrapped, with nothing lost.
    for _, value in clauses:
        assert value.startswith("*") and value.endswith("*")
        assert payload.replace("%", "\\%").replace("_", "\\_") == value[1:-1]


def test_like_wildcards_are_neutralised():
    """A bare `%` used to match every patient in the clinic.

    The expected output looks over-escaped and is not: PostgREST unescapes
    `\\\\` to `\\`, leaving SQL the pattern `%\\%%`, which PostgreSQL reads as a
    literal per cent. Both layers are asserted here.
    """
    assert pgrst.ilike_contains("%") == '"*\\\\%*"'
    assert pgrst.ilike_contains("a_b") == '"*a\\\\_b*"'

    # After PostgREST unescaping, the SQL LIKE pattern escapes the wildcard.
    postgrest_unescaped = pgrst.ilike_contains("%")[1:-1].replace("\\\\", "\\")
    sql_pattern = postgrest_unescaped.replace("*", "%")
    assert sql_pattern == "%\\%%"


def test_or_ilike_shape():
    assert pgrst.or_ilike("ash", "name", "phone", "uhid") == (
        '(name.ilike."*ash*",phone.ilike."*ash*",uhid.ilike."*ash*")'
    )


def test_eq_quotes_identifiers():
    assert pgrst.eq("abc-123") == 'eq."abc-123"'
    # An id containing a comma cannot become a second filter clause.
    assert pgrst.eq("a,b") == 'eq."a,b"'
