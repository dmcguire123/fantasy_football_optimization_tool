"""
Our own projection model, for this week and for the rest of the season.

The same features and the same model settings the backtest graded
(features.py, backtest.make_model), trained on every finished game since
2013. One model per position.

This week: every player with a game gets a prediction from his features
going into it, including this week's Vegas line and injury report.

Rest of season: each remaining week is predicted from what we know now
about the player (his usage and production so far), paired with that
week's actual game: opponent, home or away, and the Vegas line where one is
posted. Lines are only posted about a week ahead, so later weeks use the
team's average implied total and spread so far. Injury-report features are
set to "no report", since nobody knows yet. A bye week scores zero.

On its own the model is less accurate than the experts, so it never
replaces them. For this week the service nudges the blend toward it by the
weight the backtest chose. Its rest-of-season total is shown next to the
other sources, but it has not been backtested, so it stays out of the ROS
blend.
"""

import logging

import polars as pl

from . import history
from .backtest import FIRST_TRAIN_SEASON, POSITIONS, make_model
from .features import (
    build_features,
    defense_ratios,
    feature_columns,
    team_game_lines,
)


log = logging.getLogger(__name__)

# Columns that describe the game rather than the player. For a future week
# they are replaced with that week's game.
GAME_COLUMNS = [
    "week",
    "opponent_team",
    "implied_total",
    "team_spread",
    "is_home",
    "opp_allowed_ratio",
]
# Injury-report columns, unknown for future weeks.
REPORT_COLUMNS = [
    "report_status",
    "practice_status",
    "vacated_target_share",
    "vacated_carry_share",
]


# Every remaining game for each team, with Vegas numbers filled in where
# they have not been posted: the team's average so far, or the league's.
def future_games(schedules, season, first_week, final_week):
    games = team_game_lines(schedules).filter(pl.col("season") == season)
    team_average = games.group_by("team").agg(
        pl.col("implied_total").mean().alias("avg_total"),
        pl.col("team_spread").mean().alias("avg_spread"),
    )
    league_total = games["implied_total"].mean() or 22.0
    return (
        games.filter((pl.col("week") >= first_week) & (pl.col("week") <= final_week))
        .join(team_average, on="team", how="left")
        .with_columns(
            pl.coalesce("implied_total", "avg_total", pl.lit(league_total)).alias("implied_total"),
            pl.coalesce("team_spread", "avg_spread", pl.lit(0.0)).alias("team_spread"),
        )
        .drop("avg_total", "avg_spread")
    )


# Feature rows for each player's remaining games after this week: his
# player features as of now, joined to each future game.
def future_rows(features, current, schedules, season, week, final_week):
    if final_week <= week:
        return current.clear()
    games = future_games(schedules, season, week + 1, final_week)

    # Each defense's strength as of now.
    now = season * 100 + week
    defenses = (
        defense_ratios(features)
        .filter(pl.col("when") < now)
        .sort("when")
        .group_by(["opponent_team", "position"])
        .agg(pl.col("opp_allowed_ratio").last())
    )

    base = current.drop(GAME_COLUMNS)
    rows = (
        base.join(games.drop("season"), on="team", how="inner")
        .join(defenses, on=["opponent_team", "position"], how="left")
        .with_columns(
            *[pl.lit(0.0).alias(c) for c in REPORT_COLUMNS if c in current.columns],
            pl.lit(1).cast(current.schema["weeks_since_last"]).alias("weeks_since_last"),
        )
    )
    return rows.select(current.columns)


# Predict this week and every remaining week. Returns two frames keyed by
# nflverse (gsis) id: this week's projection, and the rest-of-season total
# (this week included) with the number of games it covers.
def predict(season, week, scoring, final_week=None):
    seasons = range(history.FIRST_STATS_SEASON, season + 1)
    schedules = history.load_schedules()
    features = build_features(
        history.load_player_stats(seasons),
        history.load_opportunity(seasons),
        schedules,
        scoring,
        history.load_injuries(seasons),
        upcoming=(season, week),
    )
    columns = feature_columns(features)
    is_past = (pl.col("season") < season) | (
        (pl.col("season") == season) & (pl.col("week") < week)
    )
    current = features.filter(
        (pl.col("season") == season) & (pl.col("week") == week) & pl.col("pts").is_null()
    )
    future = future_rows(features, current, schedules, season, week, final_week or week)

    weekly = []
    remaining = []
    for position in POSITIONS:
        train = features.filter(
            (pl.col("position") == position)
            & (pl.col("season") >= FIRST_TRAIN_SEASON)
            & pl.col("pts").is_not_null()
            & (pl.col("career_games") > 0)
            & is_past
        )
        this_week = current.filter(pl.col("position") == position)
        later = future.filter(pl.col("position") == position)
        if train.is_empty() or (this_week.is_empty() and later.is_empty()):
            continue
        model = make_model()
        model.fit(train.select(columns).to_numpy(), train["pts"].to_numpy())

        for rows, out in ((this_week, weekly), (later, remaining)):
            if rows.is_empty():
                continue
            out.append(
                rows.select("player_id", "name", "position", "team", "week").with_columns(
                    pl.Series("model", model.predict(rows.select(columns).to_numpy()))
                )
            )

    weekly = pl.concat(weekly) if weekly else pl.DataFrame()
    games = pl.concat([weekly] + remaining) if weekly.height or remaining else pl.DataFrame()
    if games.is_empty():
        return weekly, games
    ros = games.group_by("player_id").agg(
        pl.col("name").first(),
        pl.col("position").first(),
        pl.col("team").first(),
        pl.col("model").sum().alias("model"),
        pl.len().alias("games"),
    )
    return weekly, ros


# This week's projections only.
def predict_week(season, week, scoring):
    weekly, _ = predict(season, week, scoring)
    return weekly
