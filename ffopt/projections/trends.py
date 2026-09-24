"""
How each player's projections have moved this season, by source.

Two kinds of history:

  Weekly: for every week played so far and this week, what each source
  projected and what the player actually scored. ESPN's and Sleeper's past
  weekly projections come from their own feeds (history.py), so this works
  from the start of the season. FantasyPros, our model, and the blend come
  from our archive (store.py), so they begin when archiving began.

  Rest of season: each source's rest-of-season total, one point per day it
  was pulled. Nobody publishes old rest-of-season numbers, so this history
  starts the first time the app archived them and grows with every pull.

Movers are built from both: players whose rest-of-season blend moved the
most since last week, and players whose weekly projection this week jumped
above or below their earlier weeks (a role change, usually).

Everything is keyed by ESPN player id.
"""

import datetime
import json
import logging
import statistics

from . import history, store
from .consensus import blend


log = logging.getLogger(__name__)

WEEKLY_SOURCES = ("espn", "sleeper", "fantasypros", "model", "consensus")
ROS_SOURCES = {
    "espn_ros": "espn",
    "sleeper_ros": "sleeper",
    "fantasypros_ros": "fantasypros",
    "model_ros": "model",
    "consensus_ros": "consensus",
}

# A weekly projection has to move at least this much, and the player has to
# matter, to count as a mover.
MIN_WEEKLY_MOVE = 3.0
MIN_ROS_MOVE = 8.0
MIN_RELEVANT = 5.0


class TrendData:
    """Weekly and rest-of-season history for every player, for one season."""

    def __init__(self, season, week):
        self.season = season
        self.week = week
        # espn id -> {week: {source: points, "actual": points}}
        self.weekly = {}
        # espn id -> {source: [(date, points)]}
        self.ros = {}
        # espn id -> {"name", "position", "team"}
        self.players = {}

    def _week(self, espn_id, week):
        return self.weekly.setdefault(str(espn_id), {}).setdefault(int(week), {})

    def note_player(self, espn_id, name=None, position=None, team=None):
        info = self.players.setdefault(str(espn_id), {})
        for key, value in (("name", name), ("position", position), ("team", team)):
            if value and not info.get(key):
                info[key] = value

    # One player's history, ready to show or chart.
    def player(self, espn_id):
        espn_id = str(espn_id)
        weeks = []
        for week in sorted(self.weekly.get(espn_id, {})):
            row = {"week": week, **self.weekly[espn_id][week]}
            experts = {s: row[s] for s in ("espn", "sleeper", "fantasypros") if s in row}
            if "consensus" not in row and experts:
                row["experts_blend"] = round(blend(experts)[0], 2)
            weeks.append(row)
        ros = {
            source: [{"date": day, "points": round(points, 1)} for day, points in series]
            for source, series in self.ros.get(espn_id, {}).items()
        }
        return {"player_id": espn_id, **self.players.get(espn_id, {}), "weeks": weeks, "ros": ros}


# ------------------------------------------------------------ loading


# Past weekly projections from ESPN's and Sleeper's own feeds, scored with
# the league's rules.
def _load_source_history(data, season, scoring, ids):
    try:
        espn = history.load_espn_projections(season)
        for row in espn.iter_rows(named=True):
            if row["week"] > data.week:
                continue
            stats = {int(k): v for k, v in json.loads(row["stats"]).items()}
            data._week(row["source_id"], row["week"])["espn"] = round(
                scoring.score(stats, row["position"]), 2
            )
            data.note_player(row["source_id"], position=row["position"])
    except Exception as error:
        log.warning("ESPN weekly history unavailable: %s", error)

    try:
        sleeper = history.load_sleeper_projections(season, weeks=range(1, data.week + 1))
        for row in sleeper.iter_rows(named=True):
            if row["week"] > data.week:
                continue
            espn_id = ids.espn_id("sleeper_id", row["source_id"])
            if not espn_id:
                continue
            data._week(espn_id, row["week"])["sleeper"] = round(
                scoring.score_named(json.loads(row["stats"]), row["position"]), 2
            )
    except Exception as error:
        log.warning("Sleeper weekly history unavailable: %s", error)


# What each player actually scored, from nflverse game logs.
def _load_actuals(data, season, scoring, ids):
    try:
        stats = history.load_player_stats([season]).with_columns(
            history.points_expression(scoring).alias("pts")
        )
    except Exception as error:
        log.warning("actual points unavailable: %s", error)
        return
    columns = ["player_id", "player_display_name", "position", "team", "week", "pts"]
    for row in stats.select(columns).iter_rows(named=True):
        espn_id = ids.espn_id("gsis_id", row["player_id"])
        if not espn_id:
            continue
        data._week(espn_id, row["week"])["actual"] = round(row["pts"], 2)
        data.note_player(espn_id, row["player_display_name"], row["position"], row["team"])


# FantasyPros, the model, and the blend come from our archive: the latest
# pull of each week. Rest-of-season totals: the latest pull of each day.
def _load_archive(data, connection, season):
    rows = connection.execute(
        "SELECT source, week, espn_id, name, position, team, points, fetched_at "
        "FROM raw_projections WHERE season = ? AND espn_id IS NOT NULL "
        "ORDER BY fetched_at",
        (season,),
    ).fetchall()

    ros_by_day = {}
    for row in rows:
        source = row["source"]
        espn_id = str(row["espn_id"])
        data.note_player(espn_id, row["name"], row["position"], row["team"])
        if source in ("fantasypros", "model", "consensus"):
            # Later pulls overwrite earlier ones, leaving the latest.
            data._week(espn_id, row["week"])[source] = round(row["points"], 2)
        elif source in ROS_SOURCES:
            day = datetime.date.fromtimestamp(row["fetched_at"]).isoformat()
            ros_by_day[(espn_id, ROS_SOURCES[source], day)] = row["points"]

    for (espn_id, source, day), points in sorted(ros_by_day.items()):
        data.ros.setdefault(espn_id, {}).setdefault(source, []).append((day, points))


def load_trends(season, week, scoring, ids, db_path=store.DEFAULT_DB_PATH):
    data = TrendData(season, week)
    _load_actuals(data, season, scoring, ids)
    _load_source_history(data, season, scoring, ids)
    connection = store.open_db(db_path)
    try:
        _load_archive(data, connection, season)
    finally:
        connection.close()
    return data


# -------------------------------------------------------------- movers


# The weekly projection to judge a week by: the blend if archived, else
# the average of whichever experts projected him.
def _week_projection(row):
    if "consensus" in row:
        return row["consensus"]
    experts = [row[s] for s in ("espn", "sleeper", "fantasypros") if s in row]
    return statistics.fmean(experts) if experts else None


# Players whose projection this week is far above or below their average
# projection over the earlier weeks. Needs at least one earlier week.
def weekly_movers(data, limit=10, only=None):
    moves = []
    for espn_id, weeks in data.weekly.items():
        if only is not None and espn_id not in only:
            continue
        now = weeks.get(data.week)
        if not now:
            continue
        current = _week_projection(now)
        earlier = [
            _week_projection(row) for week, row in weeks.items() if week < data.week
        ]
        earlier = [p for p in earlier if p is not None]
        if current is None or not earlier:
            continue
        before = statistics.fmean(earlier)
        if max(current, before) < MIN_RELEVANT:
            continue
        change = current - before
        if abs(change) >= MIN_WEEKLY_MOVE:
            moves.append({"player_id": espn_id, **data.players.get(espn_id, {}),
                          "before": round(before, 1), "now": round(current, 1),
                          "change": round(change, 1)})
    risers = sorted((m for m in moves if m["change"] > 0), key=lambda m: -m["change"])
    fallers = sorted((m for m in moves if m["change"] < 0), key=lambda m: m["change"])
    return {"risers": risers[:limit], "fallers": fallers[:limit]}


# Players whose rest-of-season blend moved the most between the latest pull
# and the latest pull at least `days` days before it.
def ros_movers(data, days=5, limit=10, only=None):
    moves = []
    for espn_id, sources in data.ros.items():
        if only is not None and espn_id not in only:
            continue
        series = sources.get("consensus") or []
        if len(series) < 2:
            continue
        last_day, last = series[-1]
        cutoff = (datetime.date.fromisoformat(last_day) - datetime.timedelta(days=days)).isoformat()
        earlier = [(day, points) for day, points in series if day <= cutoff]
        if not earlier:
            continue
        then_day, then = earlier[-1]
        if max(last, then) < MIN_RELEVANT * 5:
            continue
        change = last - then
        if abs(change) >= MIN_ROS_MOVE:
            moves.append({"player_id": espn_id, **data.players.get(espn_id, {}),
                          "since": then_day, "before": round(then, 1),
                          "now": round(last, 1), "change": round(change, 1)})
    risers = sorted((m for m in moves if m["change"] > 0), key=lambda m: -m["change"])
    fallers = sorted((m for m in moves if m["change"] < 0), key=lambda m: m["change"])
    days_of_history = 0
    for sources in data.ros.values():
        days_of_history = max(days_of_history, len(sources.get("consensus") or []))
    return {"risers": risers[:limit], "fallers": fallers[:limit],
            "days_of_history": days_of_history}
