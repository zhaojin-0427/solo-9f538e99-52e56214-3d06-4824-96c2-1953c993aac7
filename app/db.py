"""SQLite schema and connection helpers.

Storage layout
--------------
``observations``
    The editable detail: one row per *identification record*
    (``recognition_id`` is the unique external key), never one row per photo.

``tag_summaries``
    One summary record per ``(district, ear_tag)``.  ``day_counts`` is a
    canonical JSON array of ``[day, count]`` pairs sorted by day, e.g.
    ``[["2026-03-01", 2], ["2026-03-05", 1]]``.  A day appears at most once,
    so several captures of one ear tag in one district on one day are already
    collapsed to a single day at write time: statistics never merge photos or
    days at query time.
    ``first_observed_on`` / ``last_observed_on`` let a range query skip
    summary records that cannot overlap the requested window.

``app_meta``
    A monotonically increasing ``state_version``, bumped once per successful
    write transaction.  Every read returns it so callers can identify the
    committed state an answer came from.

Transaction discipline
----------------------
Every write runs in one ``BEGIN IMMEDIATE`` transaction that updates the
detail row, the affected summary record(s) and the state version together.
Any failure rolls back all of them; a failed save leaves the previous detail
and the previous numbers intact.

Every read runs in one deferred transaction.  Under WAL that is a single
stable snapshot, so all detail rows and all summary pages of one query come
from the same committed state.

Both tables carry a ``committed`` flag pinned to 1 by every read.  Rows of an
in-flight transaction are additionally invisible to other SQLite connections,
so no query can observe a half-applied write.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "resighting.db",
)

_db_path: str | None = None

#: Test-only SQL trace hook, attached to every new connection when set.
_trace_hook = None


def set_trace_hook(hook) -> None:
    """Install or clear a ``sqlite3`` trace callback (tests only)."""
    global _trace_hook
    _trace_hook = hook


def get_db_path() -> str:
    """Current database path.

    Resolution order: an explicit :func:`configure_db` value, then the
    ``DB_PATH`` environment variable, then ``<repo>/data/resighting.db``.
    """
    if _db_path is not None:
        return _db_path
    return os.environ.get("DB_PATH", _DEFAULT_DB_PATH)


def configure_db(path: str | None) -> None:
    """Point the service at another database file (used by tests)."""
    global _db_path
    _db_path = path


#: Maximum number of summary records fetched by one SQL statement/page.
MAX_SUMMARY_ROWS = 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recognition_id  TEXT    NOT NULL UNIQUE,
    ear_tag         TEXT    NOT NULL,
    district        TEXT    NOT NULL,
    observed_on     TEXT    NOT NULL,          -- ISO date, YYYY-MM-DD
    committed       INTEGER NOT NULL DEFAULT 1 CHECK (committed IN (0, 1)),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- One summary record per (district, ear_tag).  day_counts already has one key
-- per day, with the number of identification records behind that day.
CREATE TABLE IF NOT EXISTS tag_summaries (
    district           TEXT    NOT NULL,
    ear_tag            TEXT    NOT NULL,
    committed          INTEGER NOT NULL DEFAULT 1 CHECK (committed IN (0, 1)),
    first_observed_on  TEXT    NOT NULL,
    last_observed_on   TEXT    NOT NULL,
    day_counts         TEXT    NOT NULL,       -- canonical JSON [[day, count]]
    PRIMARY KEY (district, ear_tag, committed)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS app_meta (
    key             TEXT    PRIMARY KEY,
    value           INTEGER NOT NULL
) WITHOUT ROWID;

INSERT OR IGNORE INTO app_meta (key, value) VALUES ('state_version', 0);

-- Detail-side indexes: range/filter listing and the (unique) id lookup.
CREATE INDEX IF NOT EXISTS idx_observations_scope
    ON observations (district, ear_tag, observed_on, committed);
CREATE INDEX IF NOT EXISTS idx_observations_range
    ON observations (committed, observed_on, district, ear_tag);

-- Summary-side indexes: the span columns let a range query skip summary
-- records that cannot overlap the window, in district or global order.
CREATE INDEX IF NOT EXISTS idx_summaries_district_span
    ON tag_summaries (district, committed, first_observed_on, last_observed_on, ear_tag);
CREATE INDEX IF NOT EXISTS idx_summaries_span
    ON tag_summaries (committed, first_observed_on, last_observed_on, district, ear_tag);
"""


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    if _trace_hook is not None:
        conn.set_trace_callback(_trace_hook)
    return conn


def init_db(path: str | None = None) -> None:
    """Create the database file and schema if needed.  Idempotent."""
    target = path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    conn = _connect(target)
    try:
        # Journal mode is persistent in the database file, so it only needs to
        # be set when the file is created.  Doing it per connection would take
        # a lock and could fail while another connection is writing.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_transaction(path: str | None = None):
    """One atomic, serialized write transaction.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so summary maintenance
    cannot interleave with another writer.  Any exception rolls back the
    detail row, the summary record(s) and the state version together.
    """
    conn = _connect(path or get_db_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        try:
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


@contextmanager
def read_transaction(path: str | None = None):
    """One read transaction with a single WAL snapshot for all statements."""
    conn = _connect(path or get_db_path())
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
