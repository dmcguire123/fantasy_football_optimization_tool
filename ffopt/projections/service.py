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
from .consensus import MIN_RELEVANT_POINTS, blend, nudge
from .ids import PlayerIds, clean_name
from .scoring import STAT_SCORED_POSITIONS
from .sources import (
    DailyBudget,
    espn_team,
    fetch_espn_weekly,
    fetch_fantasypros_players,
    fetch_sleeper,
)


log = logging.getLogger(__name__)

# FantasyPros rows are reused for this long before a player is asked for
# again, to stay inside the personal key's 100 calls a day.
FANTASYPROS_CACHE_SECONDS = 24 * 3600

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
        # How far to nudge the experts' blend toward our model, by position.
        # Zero until a backtest has shown the model helps there.
        self.model_weights = {}
        # FantasyPros ids already added, this week and rest of season, so a
        # player fetched twice is never counted twice.
        self.fantasypros_seen = {"weekly": set(), "ros": set()}

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

    def __init__(self, settings, http=None, db_path=None, ids=None, budget=None):
        self.settings = settings
        self.http = http or httpx
        self.db_path = db_path or store.DEFAULT_DB_PATH
        self.budget = budget or DailyBudget()
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
        fetched_at = time.time()

        # This week, from Sleeper. FantasyPros is asked for player by player
        # later (add_fantasypros), because of its rate limit.
        sleeper_rows = fetch_sleeper(season, week, http=self.http)
        connection = store.open_db(self.db_path) if archive else None
        if sleeper_rows:
            projection_set.sources.add("sleeper")
            scored = self._score_rows(sleeper_rows, scoring)
            for row in scored:
                projection_set.add(
                    projection_set.weekly, "sleeper", row["espn_id"], row["name"],
                    row["position"], row["team"], row["points"],
                )
            if connection:
                store.save_pull(connection, season, week, "sleeper", scored, fetched_at)

        # FantasyPros rows pulled in the last day are reused from the archive.
        if connection:
            self._load_cached_fantasypros(connection, projection_set)

        # Our own model, when switched on and its libraries are installed.
        if self.settings.use_model:
            model_rows = self._model_rows(season, week, scoring)
            if model_rows:
                projection_set.sources.add("model")
                projection_set.model_weights = self._model_weights()
                for row in model_rows:
                    projection_set.add(
                        projection_set.weekly, "model", row["espn_id"], row["name"],
                        row["position"], row["team"], row["points"],
                    )
                if connection:
                    store.save_pull(connection, season, week, "model", model_rows, fetched_at)

        # Rest of season: Sleeper projects every remaining week, so add them
        # up. FantasyPros gives a rest-of-season total directly.
        weeks = list(range(week, max(week, final_week) + 1))
        projection_set.ros_weeks = len(weeks)
        for future_week in weeks:
            rows = sleeper_rows if future_week == week else fetch_sleeper(
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

        if connection:
            connection.close()

        self._cache[key] = (time.monotonic(), projection_set)
        return projection_set

    # FantasyPros rows already pulled for this week within the cache time,
    # from the archive, so restarts and repeat runs cost no API calls.
    def _load_cached_fantasypros(self, connection, projection_set):
        since = time.time() - FANTASYPROS_CACHE_SECONDS
        for source, table, seen in (
            ("fantasypros", projection_set.weekly, "weekly"),
            ("fantasypros_ros", projection_set.ros, "ros"),
        ):
            rows = store.recent_rows(
                connection, projection_set.season, projection_set.week, source, since
            )
            for row in rows:
                projection_set.add(
                    table, "fantasypros", row["espn_id"], row["name"],
                    row["position"], row["team"], row["points"],
                )
            projection_set.fantasypros_seen[seen].update(row["source_id"] for row in rows)
            if rows and seen == "weekly":
                projection_set.sources.add("fantasypros")

    # Fill in FantasyPros for the given players, in the order given, as far
    # as today's call budget allows: this week's projection first, then the
    # rest-of-season total. Everything fetched is archived, which is also
    # the cache.
    def add_fantasypros(self, projection_set, players, scoring, archive=True):
        api_key = self.settings.fantasypros_api_key
        if not api_key:
            return 0
        wanted = []
        for player in players:
            fp_id = self.ids().other_id(player.player_id, "fantasypros_id")
            if fp_id and fp_id not in projection_set.fantasypros_seen["weekly"]:
                wanted.append(fp_id)
        wanted = list(dict.fromkeys(wanted))
        if not wanted:
            return 0

        season, week = projection_set.season, projection_set.week
        weekly = self._score_rows(
            fetch_fantasypros_players(
                season, week, api_key, wanted, self.budget, http=self.http
            ),
            scoring,
        )
        returned = [row["source_id"] for row in weekly]
        need_ros = [fp for fp in returned if fp not in projection_set.fantasypros_seen["ros"]]
        ros = self._score_rows(
            fetch_fantasypros_players(
                season, week, api_key, need_ros, self.budget, http=self.http, ros=True
            ),
            scoring,
        )

        for row in weekly:
            projection_set.add(
                projection_set.weekly, "fantasypros", row["espn_id"], row["name"],
                row["position"], row["team"], row["points"],
            )
        for row in ros:
            projection_set.add(
                projection_set.ros, "fantasypros", row["espn_id"], row["name"],
                row["position"], row["team"], row["points"],
            )
        # Only players that came back count as done; the rest are asked for
        # again once there is budget.
        projection_set.fantasypros_seen["weekly"].update(returned)
        projection_set.fantasypros_seen["ros"].update(row["source_id"] for row in ros)
        if weekly:
            projection_set.sources.add("fantasypros")

        if archive and (weekly or ros):
            connection = store.open_db(self.db_path)
            try:
                store.save_pull(connection, season, week, "fantasypros", weekly)
                store.save_pull(connection, season, week, "fantasypros_ros", ros)
            finally:
                connection.close()
        return len(weekly)

    # The model's predictions for a week, as archive rows keyed to ESPN ids.
    def _model_rows(self, season, week, scoring):
        try:
            from .model import predict_week

            predictions = predict_week(season, week, scoring)
        except Exception as error:
            log.warning("model projections unavailable: %s", error)
            return []
        rows = []
        for record in predictions.iter_rows(named=True):
            rows.append(
                {
                    "source_id": record["player_id"],
                    "espn_id": self.ids().espn_id("gsis_id", record["player_id"]),
                    "name": record["name"],
                    "position": record["position"],
                    "team": espn_team(record["team"]),
                    "points": float(record["model"]),
                    "stats": {},
                }
            )
        return rows

    # The model's weight by position, from the last backtest run.
    def _model_weights(self):
        from .backtest import load_results

        results = load_results() or {}
        return results.get("weights") or {}

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
# and the blend is only shown alongside. The experts are blended with equal
# weight, then nudged toward our model by its backtested weight.
def apply(players, projection_set, use_consensus=True):
    for player in players:
        weekly = {"espn": player.espn_projected_points}
        weekly.update(projection_set.weekly_for(player))
        experts = {s: v for s, v in weekly.items() if s != "model"}
        mean, spread = blend(projected_only(experts))
        # The model's weight was only tested on players the experts expect
        # to play a real role; below that it does not move the blend.
        weight = projection_set.model_weights.get(player.position, 0.0)
        if "model" in weekly and weight and mean >= MIN_RELEVANT_POINTS:
            mean = nudge(mean, weekly["model"], weight)

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
            ros_mean, _ = blend(projected_only(ros))
            player.ros_points = ros_mean
            if not player.ros_games:
                player.ros_games = projection_set.ros_weeks
    return players
