"""
Pull every source, score it for this league, and attach the consensus.

The flow, once per week and cached:

  1. Fetch this week's projections from each outside source, plus every
     remaining week's for the rest-of-season total.
  2. Score each stat line with the league's rules (scoring.py).
  3. Save this week's pull to the archive (store.py).
  4. Index the rows by ESPN id, falling back to name and position, and to
     team for defenses.

Then apply() hands each ESPN Player its per-source projections, the blend,
and a rest-of-season total. When the consensus is switched on, the blend
replaces ESPN's number in projected_points, so the lineup optimizer, the
waiver logic, and win probability all use it without further changes.
"""

import logging
import time

import httpx

from . import store
from .consensus import blend
from .ids import PlayerIds, clean_name
from .scoring import STAT_SCORED_POSITIONS
from .sources import fetch_espn_weekly, fetch_fantasypros, fetch_sleeper


log = logging.getLogger(__name__)

# The id column in the DynastyProcess table for each source's player ids.
SOURCE_ID_COLUMNS = {
    "sleeper": "sleeper_id",
    "fantasypros": "fantasypros_id",
    "fantasypros_ros": "fantasypros_id",
}


# Score one source row for this league. Offensive players are scored from
# their stat line. Kickers and defenses use the source's standard-scoring
# points, since reception scoring does not touch them.
def score_row(row, scoring):
    if row.position in STAT_SCORED_POSITIONS and row.stats:
        return scoring.score_named(row.stats, row.position)
    if row.position in STAT_SCORED_POSITIONS:
        reception_points = scoring.reception_points(row.position)
        if reception_points >= 1:
            return row.points.get("ppr", 0.0)
        if reception_points >= 0.5:
            return row.points.get("half", 0.0)
    return row.points.get("std", 0.0)


class ProjectionSet:
    """Scored projections from outside sources for one week, ready to look up."""

    def __init__(self, season, week):
        self.season = season
        self.week = week
        # match key -> {source: points}, for this week and for the rest of
        # the season. Keys are ("espn", id), ("name", name, position, team),
        # and ("team", abbrev) for defenses.
        self.weekly = {}
        self.ros = {}
        self.ros_weeks = 0
        self.sources = set()

    # Record one scored row under every key it can be found by.
    def add(self, table, source, espn_id, name, position, team, points):
        keys = []
        if espn_id:
            keys.append(("espn", str(espn_id)))
        if position == "D/ST":
            keys.append(("team", team))
        else:
            keys.append(("name", clean_name(name), position, team))
        for key in keys:
            entry = table.setdefault(key, {})
            entry[source] = entry.get(source, 0.0) + points

    def _lookup(self, table, player):
        for key in (
            ("espn", str(player.player_id)),
            ("team", player.pro_team) if player.position == "D/ST" else None,
            ("name", clean_name(player.name), player.position, player.pro_team),
        ):
            if key and key in table:
                return table[key]
        return {}

    def weekly_for(self, player):
        return self._lookup(self.weekly, player)

    def ros_for(self, player):
        return self._lookup(self.ros, player)


class ProjectionService:
    """Builds and caches ProjectionSets, and applies them to players."""

    def __init__(self, settings, http=None, db_path=None, ids=None):
        self.settings = settings
        self.http = http or httpx
        self.db_path = db_path or store.DEFAULT_DB_PATH
        self._ids = ids
        self._cache = {}

    def ids(self):
        if self._ids is None:
            try:
                self._ids = PlayerIds.load(client=self.http)
            except Exception as error:
                log.warning("player id table unavailable, matching by name: %s", error)
                self._ids = PlayerIds([])
        return self._ids

    # Map a source row to an ESPN id through the id table.
    def espn_id_for(self, row):
        column = SOURCE_ID_COLUMNS.get(row.source)
        if not column:
            return None
        return self.ids().espn_id(column, row.source_id)

    # Scored rows for one source and week, as dicts ready for the archive.
    def _score_rows(self, rows, scoring):
        scored = []
        for row in rows:
            scored.append(
                {
                    "source": row.source,
                    "source_id": row.source_id,
                    "espn_id": self.espn_id_for(row),
                    "name": row.name,
                    "position": row.position,
                    "team": row.team,
                    "points": score_row(row, scoring),
                    "stats": row.stats,
                }
            )
        return scored

    # Fetch, score, and archive every source for a week. Cached for the
    # configured time, since sources update a few times a week at most.
    def build(self, season, week, final_week, scoring, archive=True):
        key = (season, week, final_week)
        ttl = self.settings.projections_ttl_seconds
        hit = self._cache.get(key)
        if hit and ttl > 0 and time.monotonic() - hit[0] < ttl:
            return hit[1]

        projection_set = ProjectionSet(season, week)
        api_key = self.settings.fantasypros_api_key
        fetched_at = time.time()

        # This week, from each source.
        weekly_pulls = {
            "sleeper": fetch_sleeper(season, week, http=self.http),
            "fantasypros": fetch_fantasypros(season, week, api_key, http=self.http),
        }
        connection = store.open_db(self.db_path) if archive else None
        for source, rows in weekly_pulls.items():
            if not rows:
                continue
            projection_set.sources.add(source)
            scored = self._score_rows(rows, scoring)
            for row in scored:
                projection_set.add(
                    projection_set.weekly, source, row["espn_id"], row["name"],
                    row["position"], row["team"], row["points"],
                )
            if connection:
                store.save_pull(connection, season, week, source, scored, fetched_at)

        # Rest of season: Sleeper projects every remaining week, so add them
        # up. FantasyPros gives a rest-of-season total directly.
        weeks = list(range(week, max(week, final_week) + 1))
        projection_set.ros_weeks = len(weeks)
        for future_week in weeks:
            rows = weekly_pulls["sleeper"] if future_week == week else fetch_sleeper(
                season, future_week, http=self.http
            )
            for row in self._score_rows(rows, scoring):
                projection_set.add(
                    projection_set.ros, "sleeper", row["espn_id"], row["name"],
                    row["position"], row["team"], row["points"],
                )

        # ESPN's remaining weeks, rescored with the league's rules.
        for espn_id, (position, weeks_by_number) in fetch_espn_weekly(
            season, http=self.http
        ).items():
            total = 0.0
            for future_week in weeks:
                stats = weeks_by_number.get(future_week)
                if stats:
                    total += scoring.score(stats, position)
            projection_set.ros.setdefault(("espn", espn_id), {})["espn"] = total

        ros_rows = fetch_fantasypros(season, week, api_key, http=self.http, ros=True)
        scored_ros = self._score_rows(ros_rows, scoring)
        for row in scored_ros:
            projection_set.add(
                projection_set.ros, "fantasypros", row["espn_id"], row["name"],
                row["position"], row["team"], row["points"],
            )
        if connection:
            if scored_ros:
                store.save_pull(connection, season, week, "fantasypros_ros", scored_ros, fetched_at)
            connection.close()

        self._cache[key] = (time.monotonic(), projection_set)
        return projection_set

    # Save ESPN's own projections for a week to the archive, so it can be
    # graded next to the other sources later.
    def archive_espn(self, season, week, players):
        rows = []
        seen = set()
        for player in players:
            if player.player_id in seen:
                continue
            seen.add(player.player_id)
            rows.append(
                {
                    "source_id": str(player.player_id),
                    "espn_id": str(player.player_id),
                    "name": player.name,
                    "position": player.position,
                    "team": player.pro_team,
                    "points": player.espn_projected_points,
                    "stats": {},
                }
            )
        connection = store.open_db(self.db_path)
        try:
            return store.save_pull(connection, season, week, "espn", rows)
        finally:
            connection.close()


# A source that projects exactly zero while another projects points has not
# really projected the player; ESPN does this for backups who have just
# been promoted to starter. Leave those zeros out of the blend. When every
# source says zero (a bye, an injury), the zeros stand.
def projected_only(source_points):
    positive = {s: v for s, v in source_points.items() if v and v > 0}
    return positive or source_points


# Give each player every source's projection, the blend, and a
# rest-of-season total. With use_consensus, the blend becomes the player's
# projected_points and ros_points; otherwise ESPN's numbers stay in place
# and the blend is only shown alongside.
def apply(players, projection_set, use_consensus=True, weights=None):
    for player in players:
        weekly = {"espn": player.espn_projected_points}
        weekly.update(projection_set.weekly_for(player))
        mean, spread = blend(projected_only(weekly), weights)
        player.source_points = weekly
        player.consensus_points = mean
        player.consensus_spread = spread
        if use_consensus and len(weekly) > 1:
            player.projected_points = mean

        ros = {}
        if player.ros_games:
            ros["espn"] = player.ros_points
        ros.update(projection_set.ros_for(player))
        if use_consensus and ros:
            ros_mean, _ = blend(projected_only(ros), weights)
            player.ros_points = ros_mean
            if not player.ros_games:
                player.ros_games = projection_set.ros_weeks
    return players

