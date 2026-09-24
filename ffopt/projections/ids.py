"""
Match the same player across ESPN, Sleeper, FantasyPros, and nflverse.

Each site has its own player ids. The DynastyProcess id table maps between
them for most players. Rookies and fringe players are sometimes missing from
it, so a match on cleaned-up name, position, and team is the fallback.
"""

import csv
import io
import re

from ..nflverse import DEFAULT_CACHE_DIR, ID_MAP_TTL, ID_MAP_URL, _fetch_cached


# The id columns we care about in the DynastyProcess table.
ID_COLUMNS = ("espn_id", "sleeper_id", "fantasypros_id", "gsis_id")

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


# Lowercase, drop punctuation and suffixes, so "D.K. Metcalf Jr." and
# "DK Metcalf" come out the same.
def clean_name(name):
    words = re.sub(r"[^a-z ]", "", (name or "").lower().replace("-", " ")).split()
    words = [w for w in words if w not in NAME_SUFFIXES]
    return "".join(words)


def _usable(value):
    value = (value or "").strip()
    if not value or value == "NA":
        return ""
    return value


class PlayerIds:
    """Look up a player's ESPN id from any other site's id, or by name."""

    def __init__(self, rows):
        # (column, id) -> espn id
        self.to_espn = {}
        # espn id -> {column: id}
        self.from_espn = {}
        for row in rows:
            espn_id = _usable(row.get("espn_id"))
            if not espn_id:
                continue
            ids = {}
            for column in ID_COLUMNS:
                value = _usable(row.get(column))
                if value:
                    ids[column] = value
                    self.to_espn[(column, value)] = espn_id
            self.from_espn[espn_id] = ids

    # Load the id table, reusing the copy the spread model already caches.
    @classmethod
    def load(cls, cache_dir=DEFAULT_CACHE_DIR, client=None):
        text = _fetch_cached(
            ID_MAP_URL, cache_dir / "db_playerids.csv", ID_MAP_TTL, client=client
        )
        return cls(csv.DictReader(io.StringIO(text)))

    # ESPN id for another site's id, or None.
    def espn_id(self, column, value):
        value = _usable(str(value) if value is not None else "")
        if not value:
            return None
        return self.to_espn.get((column, value))

    # Another site's id for an ESPN id, or None.
    def other_id(self, espn_id, column):
        return self.from_espn.get(str(espn_id), {}).get(column)

