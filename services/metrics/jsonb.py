"""
JSON column compatibility.

``models/document.py`` declares ``ai_analysis``, ``keywords``,
``file_metadata`` and ``embedding_provenance`` as ``JSONB``, and the squashed
migration creates them that way — but the deployed database has at least some
of them as plain ``json``. ``services/search_service.py`` has known this for a
long time; it casts ``documents.keywords::jsonb`` in four places, each with the
comment "the keywords column is json type".

The distinction is invisible for the operators the two types share. ``->`` and
``->>`` exist for both, which is why the summary and token-cost reads work
untouched. It is fatal for the ``jsonb``-only functions:

    jsonb_exists(json, text)                      → function does not exist
    jsonb_array_length(json)                      → function does not exist
    COALESCE(json_value, '[]'::jsonb)             → types json and jsonb
                                                    cannot be matched

Casting explicitly is correct in both worlds — ``jsonb::jsonb`` is a no-op —
so every ``jsonb``-only call in the metrics layer goes through these helpers
rather than touching the column directly.

The real fix is a migration that settles the column types, at which point
these helpers become redundant no-ops rather than wrong. Until then, matching
what search_service already does keeps one convention across the codebase.
"""

from sqlalchemy import Text, cast, func, literal
from sqlalchemy.dialects.postgresql import JSONB


def as_jsonb(column):
    """
    A JSON column as ``jsonb``, whichever type it actually has.

    Use for any expression reaching a ``jsonb``-only function. Reads that use
    only ``->`` / ``->>`` do not need it and are left alone, so the cast is
    not paid where it buys nothing.
    """
    return cast(column, JSONB)


def empty_array():
    """``'[]'::jsonb`` as a bound literal, for COALESCE over a missing key."""
    return cast(literal("[]"), JSONB)


def get(column, key: str):
    """
    ``column -> key`` — the JSON value at ``key``.

    Explicitly ``->`` rather than ``column[key]``: the ORM would render the
    latter as subscript syntax, which needs PostgreSQL 14+.
    """
    return column.op("->")(key)


def get_text(column, key: str):
    """``column ->> key`` — the value at ``key`` as text."""
    return column.op("->>", return_type=Text)(key)


def has_key(column, key: str):
    """
    ``key`` is present at the top level of ``column``.

    ``jsonb_exists`` rather than the ``?`` operator: ``?`` is also the qmark
    paramstyle marker, so mixing it with a bound parameter in one statement is
    a driver-dependent footgun.
    """
    return func.jsonb_exists(as_jsonb(column), key)


def array_length(column, key: str):
    """Length of the array at ``column -> key``, or 0 when the key is absent."""
    return func.jsonb_array_length(
        func.coalesce(get(as_jsonb(column), key), empty_array())
    )
