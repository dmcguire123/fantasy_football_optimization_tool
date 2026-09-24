"""
An on-disk archive of every projection pull.

No source keeps its old weekly projections around for long, and without
them there is no way to check later which source was right. So every pull
is saved here, one row per player per source per pull, already scored with
the league's rules. Pulls are never overwritten: a Tuesday pull and a Sunday
pull for the same week are both kept, stamped with when they were taken.
"""

import json
import sqlite3
import time
from pathlib import Path

from ..config import PROJECT_ROOT


DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "projections.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_projections (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    espn_id TEXT,
    name TEXT,
    position TEXT,
    team TEXT,
    points REAL,
    stats_json TEXT,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (season, week, source, source_id, fetched_at)
);
CREATE INDEX IF NOT EXISTS raw_projections_week
    ON raw_projections (season, week, source);
"""


def open_db(path=DEFAULT_DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


# Save one source's scored projections for a week. Each row is a dict with
# source_id, espn_id, name, position, team, points, and stats.
def save_pull(connection, season, week, source, rows, fetched_at=None):
    fetched_at = fetched_at or time.time()
    connection.executemany(
        "INSERT OR REPLACE INTO raw_projections VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                season,
                week,
                source,
                row["source_id"],
                row.get("espn_id"),
                row.get("name"),
                row.get("position"),
                row.get("team"),
                row.get("points"),
                json.dumps(row.get("stats") or {}, sort_keys=True),
                fetched_at,
            )
            for row in rows
        ],
    )
    connection.commit()
    return len(rows)


# The most recent pull of each source for a week, optionally only pulls
# taken before a cutoff time (for example, kickoff).
def latest_pull(connection, season, week, before=None):
    before = before if before is not None else time.time() + 1
    cursor = connection.execute(
        """
        SELECT r.* FROM raw_projections r
        JOIN (
            SELECT source, MAX(fetched_at) AS fetched_at
            FROM raw_projections
            WHERE season = ? AND week = ? AND fetched_at < ?
            GROUP BY source
        ) last
          ON r.source = last.source AND r.fetched_at = last.fetched_at
        WHERE r.season = ? AND r.week = ?
        """,
        (season, week, before, season, week),
    )
    return [dict(row) for row in cursor.fetchall()]


# How many rows each source has, by week, for a quick look at the archive.
def summary(connection, season=None):
    query = (
        "SELECT season, week, source, COUNT(DISTINCT fetched_at) AS pulls, "
        "COUNT(*) AS rows FROM raw_projections"
    )
    params = ()
    if season:
        query += " WHERE season = ?"
        params = (season,)
    query += " GROUP BY season, week, source ORDER BY season, week, source"
    return [dict(row) for row in connection.execute(query, params).fetchall()]
