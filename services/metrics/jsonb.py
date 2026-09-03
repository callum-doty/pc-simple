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

import logging
from typing import Optional

from sqlalchemy import Text, cast, func, literal, text
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)

#: Whether the deployed columns still need a ``::jsonb`` cast.
#:
#: None means "not yet checked", and the helpers cast in that state — the
#: cautious default, since casting a jsonb column is a no-op while failing to
#: cast a json one is a 500.
#:
#: Detected once per process rather than assumed, because the conversion is a
#: table rewrite that needs a maintenance window (see
#: scripts/convert_json_to_jsonb.py). Tying correctness to whether that has
#: happened yet is what broke the last deploy: the migration could not take its
#: lock, the build did not stop, and code that had dropped its casts went live
#: against unconverted columns.
_needs_cast: Optional[bool] = None

#: Columns the model declares as JSONB.
_JSON_COLUMNS = ("ai_analysis", "keywords", "file_metadata", "embedding_provenance")


def configure(db) -> bool:
    """
    Detect once whether the deployed JSON columns need casting.

    Cheap: one catalog query per process, cached thereafter. Call it before
    building any JSON predicate — the metrics endpoints do this on entry.

    Returns True when a cast is still required.
    """
    global _needs_cast
    if _needs_cast is not None:
        return _needs_cast
    try:
        rows = db.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = \'public\' AND table_name = \'documents\' "
                "AND column_name = ANY(:cols)"
            ),
            {"cols": list(_JSON_COLUMNS)},
        ).fetchall()
    except Exception as e:
        logger.warning(f"Metrics: could not detect JSON column types, casting: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        _needs_cast = True
        return _needs_cast

    plain = [name for name, kind in rows if kind == "json"]
    _needs_cast = bool(plain)
    if plain:
        logger.info(
            "Metrics: %s still json, casting to jsonb per query. Run "
            "scripts/convert_json_to_jsonb.py during a maintenance window to "
            "remove this cost.",
            ", ".join(sorted(plain)),
        )
    return _needs_cast


def needs_cast_sql() -> str:
    """
    ``"::jsonb"`` or ``""``, for raw-SQL callers that cannot use the
    expression helpers.
    """
    return "" if _needs_cast is False else "::jsonb"


def reset_detection() -> None:
    """Forget the cached detection. For tests, and after a conversion."""
    global _needs_cast
    _needs_cast = None


def as_jsonb(column):
    """
    A JSON column as ``jsonb``, whichever type it actually has.

    A no-op once the columns are genuinely jsonb — which matters, because the
    cast is not free: ``json -> jsonb`` re-parses the whole document for every
    row, and a cast also makes any index on the column unusable.

    Reads that use only ``->`` / ``->>`` never need this and are left alone.
    """
    if _needs_cast is False:
        return column
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

    Cast only while the columns are still ``json`` — see ``as_jsonb``.
    """
    return func.jsonb_exists(as_jsonb(column), key)


def array_length(column, key: str):
    """Length of the array at ``column -> key``, or 0 when the key is absent."""
    return func.jsonb_array_length(
        func.coalesce(get(as_jsonb(column), key), empty_array())
    )
