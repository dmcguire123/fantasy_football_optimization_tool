"""
A local record of what the model predicted, and what actually happened.

Every matchup in the league is a chance to check the win-probability model:
before the games, note each side's projected score and spread and the chance
that gives the home team; afterward, note who actually won. Twelve teams make
six games a week, so a few weeks of this is enough to see whether "70%" really
means about 70%.

Alongside the real prediction, each row also keeps what the old fixed-spread
model would have said, so the two can be compared on the same games.

Stored in SQLite under data/, next to the nflverse cache.
"""

import math
import sqlite3
import time
from pathlib import Path

from .winprob import lineup_distribution, win_probability


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    home_team_id INTEGER NOT NULL,
    away_team_id INTEGER NOT NULL,
    home_mean REAL NOT NULL,
    home_sd REAL NOT NULL,
    away_mean REAL NOT NULL,
    away_sd REAL NOT NULL,
    p_home REAL NOT NULL,
    p_home_flat REAL NOT NULL,
    taken_at REAL NOT NULL,
    home_score REAL,
    away_score REAL,
    final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, week, home_team_id, away_team_id)
)
"""


def open_db(path=None):
    path = Path(path or DEFAULT_DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


# Record the model's view of every matchup in one week, as the lineups are set
# right now. Running it again later in the week replaces the earlier view with
# a better-informed one, but a finished game is never touched.
def record_predictions(conn, league, season, week=None, now=None):
    week = week or league.week
    now = now if now is not None else time.time()
    recorded = 0

    for matchup in league.matchups:
        if matchup.week != week:
            continue
        home = league.team_by_id(matchup.home_team_id)
        away = league.team_by_id(matchup.away_team_id)
        if not home or not away:
            continue

        home_mean, home_sd = lineup_distribution(home.starters)
        away_mean, away_sd = lineup_distribution(away.starters)
        p_home = win_probability(home_mean, away_mean, home_sd, away_sd)
        p_flat = win_probability(home_mean, away_mean)

        cursor = conn.execute(
            """
            INSERT INTO predictions (
                season, week, home_team_id, away_team_id, home_mean, home_sd,
                away_mean, away_sd, p_home, p_home_flat, taken_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (season, week, home_team_id, away_team_id) DO UPDATE SET
                home_mean = excluded.home_mean, home_sd = excluded.home_sd,
                away_mean = excluded.away_mean, away_sd = excluded.away_sd,
                p_home = excluded.p_home, p_home_flat = excluded.p_home_flat,
                taken_at = excluded.taken_at
            WHERE predictions.final = 0
            """,
            (
                season, week, home.team_id, away.team_id, home_mean, home_sd,
                away_mean, away_sd, p_home, p_flat, now,
            ),
        )
        recorded += cursor.rowcount
    conn.commit()
    return recorded


# Fill in the scores for finished weeks. Only matchups we already made a
# prediction for are touched, and only once the week is over.
def record_results(conn, league, season):
    settled = 0
    for matchup in league.matchups:
        if matchup.week >= league.week:
            continue
        cursor = conn.execute(
            """
            UPDATE predictions SET home_score = ?, away_score = ?, final = 1
            WHERE season = ? AND week = ? AND home_team_id = ? AND away_team_id = ?
              AND final = 0
            """,
            (
                matchup.home_score, matchup.away_score, season, matchup.week,
                matchup.home_team_id, matchup.away_team_id,
            ),
        )
        settled += cursor.rowcount
    conn.commit()
    return settled


# Take a snapshot now: predict this week, settle any finished weeks.
def snapshot(conn, league, season):
    return {
        "week": league.week,
        "predicted": record_predictions(conn, league, season),
        "settled": record_results(conn, league, season),
    }


# Rebuild predictions for weeks already played, using the lineups ESPN reports
# for each week. `load_week` takes a week number and returns that week's
# League. Where ESPN kept the projections from before the games this is a fair
# backtest; where it only keeps hindsight it will flatter the model.
def backfill(conn, load_week, season, current_week):
    predicted = 0
    for week in range(1, current_week):
        predicted += record_predictions(conn, load_week(week), season, week=week)
    return predicted


# The observations for scoring the model: every finished game counted from
# both sides, as (predicted chance, flat-spread chance, won). A tie counts as
# half a win.
def _observations(conn):
    rows = conn.execute(
        "SELECT p_home, p_home_flat, home_score, away_score FROM predictions WHERE final = 1"
    ).fetchall()
    observations = []
    for row in rows:
        if row["home_score"] > row["away_score"]:
            home_won = 1.0
        elif row["home_score"] < row["away_score"]:
            home_won = 0.0
        else:
            home_won = 0.5
        observations.append((row["p_home"], row["p_home_flat"], home_won))
        observations.append((1 - row["p_home"], 1 - row["p_home_flat"], 1 - home_won))
    return observations


def _brier(pairs):
    return sum((p - won) ** 2 for p, won in pairs) / len(pairs)


def _log_loss(pairs):
    eps = 1e-6
    total = 0.0
    for p, won in pairs:
        p = min(1 - eps, max(eps, p))
        total -= won * math.log(p) + (1 - won) * math.log(1 - p)
    return total / len(pairs)


# How well the predictions held up: overall scores for the measured-spread
# model against the fixed-spread one, and a reliability table showing, for
# each band of predicted chance, how often that side really won. A coin flip
# scores 0.25 on Brier and 0.693 on log loss, so lower than that is signal.
def calibration(conn, bins=5):
    observations = _observations(conn)
    games = len(observations) // 2
    if not observations:
        return {"games": 0, "bins": []}

    model = [(p, won) for p, _, won in observations]
    flat = [(p, won) for _, p, won in observations]

    table = []
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        inside = [
            (p, won) for p, won in model
            if low <= p < high or (index == bins - 1 and p == 1.0)
        ]
        if not inside:
            continue
        table.append(
            {
                "low": round(low, 2),
                "high": round(high, 2),
                "count": len(inside),
                "predicted": round(sum(p for p, _ in inside) / len(inside), 3),
                "actual": round(sum(w for _, w in inside) / len(inside), 3),
            }
        )

    return {
        "games": games,
        "brier": round(_brier(model), 4),
        "brier_flat": round(_brier(flat), 4),
        "log_loss": round(_log_loss(model), 4),
        "log_loss_flat": round(_log_loss(flat), 4),
        "bins": table,
    }


# Games with a prediction still waiting on a result.
def pending_count(conn):
    return conn.execute("SELECT COUNT(*) FROM predictions WHERE final = 0").fetchone()[0]


# Snapshot the league through a LeagueService: optionally rebuild the weeks
# already played first, then record this week and settle finished games.
def run_snapshot(service, conn, do_backfill=False):
    league = service.load_league()
    season = service.settings.season

    backfilled = 0
    if do_backfill:
        backfilled = backfill(conn, service.load_league, season, league.week)

    result = snapshot(conn, league, season)
    result["backfilled"] = backfilled
    return result
