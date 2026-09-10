"""Safe construction of PostgREST filter strings.

PostgREST filters are a small text grammar, not bound parameters. The
patient search used to build them by interpolation::

    or=(name.ilike.*{q}*,phone.ilike.*{q}*,uhid.ilike.*{q}*)

A query containing ``)`` or ``,`` closes the group early and the rest of
the user's text is parsed as further filter clauses. Row-Level Security
still contains the blast radius to the caller's own clinic, but the query
the database runs is no longer the one we wrote — which is the definition
of an injection.

PostgREST's own escaping rule is: wrap a value in double quotes and
backslash-escape any ``"`` or ``\\`` inside it. Everything else — commas,
parentheses, dots — is then literal.
"""
from __future__ import annotations

# Characters PostgREST treats as wildcards inside ilike/like patterns.
_LIKE_WILDCARDS = str.maketrans({"%": r"\%", "_": r"\_"})


def quote(value: str) -> str:
    """Quote a value for use in a PostgREST filter."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def ilike_contains(value: str) -> str:
    r"""A quoted ``*value*`` pattern with the caller's own wildcards neutralised.

    There are two layers of escaping here and they compose, which is easy to
    get backwards:

    1. SQL ``LIKE`` treats ``%`` and ``_`` as wildcards, and a backslash as the
       default escape character. To match a literal ``%`` the SQL pattern must
       contain ``\%``.
    2. PostgREST unescapes ``\\`` to ``\`` inside a double-quoted value.

    So a literal ``%`` has to leave here as ``\\%``: PostgREST turns that into
    ``\%``, and PostgreSQL reads that as a literal per cent. Verified against a
    real PostgreSQL in ``tests/test_pgrst_filters.py``.

    Without this, a search for ``%`` produces the pattern ``%%%``, which matches
    every patient in the clinic — an information disclosure sitting on top of
    the injection.
    """
    return quote(f"*{str(value).translate(_LIKE_WILDCARDS)}*")


def or_ilike(value: str, *columns: str) -> str:
    """``or=(col.ilike."*v*",col2.ilike."*v*")`` — safely quoted."""
    pattern = ilike_contains(value)
    return "(" + ",".join(f"{c}.ilike.{pattern}" for c in columns) + ")"


def eq(value: str) -> str:
    """``col=eq."value"`` — used for ids, which are UUIDs but are still
    caller-controlled strings until the database parses them."""
    return f"eq.{quote(value)}"
