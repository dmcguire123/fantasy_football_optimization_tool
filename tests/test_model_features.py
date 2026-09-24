"""
Model features and backtest bookkeeping, on small synthetic seasons.

The most important property here is that no feature for a game can see
that game or any later one. If it could, the backtest would flatter the
model and the live numbers would disappoint.
"""

import polars as pl
import pytest

from ffopt.projections import backtest
from ffopt.projections.features import build_features, feature_columns
from ffopt.projections.history import NFLVERSE_STATS, points_expression
from ffopt.projections.scoring import LeagueScoring


SCORING = LeagueScoring.default_ppr()

STAT_COLUMNS = sorted(
    {c for columns in NFLVERSE_STATS.values() for c in columns}
    | {"target_share", "air_yards_share", "wopr"}
)


# A game log: one WR and one RB on DET, and an RB on CHI, for weeks 1-6 of
# two seasons. Each row's stats come from the week number, so later weeks
# can be changed and checked for leaks.
def make_stats(bump_from=None):
    rows = []
    for season in (2023, 2024):
        for week in range(1, 7):
            for player_id, position, team, opponent in (
                ("wr1", "WR", "DET", "CHI"),
                ("rb1", "RB", "DET", "CHI"),
                ("rb2", "RB", "CHI", "DET"),
            ):
                row = {c: 0.0 for c in STAT_COLUMNS}
                boost = 50.0 if bump_from and (season, week) >= bump_from else 0.0
                row.update(
                    player_id=player_id,
                    player_display_name=player_id.upper(),
                    position=position,
                    season=season,
                    week=week,
                    season_type="REG",
                    team=team,
                    opponent_team=opponent,
                    targets=5.0 + week,
                    receptions=4.0 + boost,
                    receiving_yards=40.0 + week * 5,
                    carries=10.0 if position == "RB" else 0.0,
                    rushing_yards=45.0 if position == "RB" else 0.0,
                    target_share=0.2,
                )
                rows.append(row)
    return pl.DataFrame(rows)


def make_opportunity(stats):
    return stats.select(
        "player_id",
        "season",
        "week",
        (pl.col("targets") * 1.5).alias("total_fantasy_points_exp"),
        pl.lit(0.0).alias("rush_fantasy_points_exp"),
        (pl.col("targets") * 1.5).alias("rec_fantasy_points_exp"),
        pl.lit(0.0).alias("pass_fantasy_points_exp"),
        pl.lit(25.0).alias("rush_attempt_team"),
    )


def make_schedules():
    rows = []
    for season in (2023, 2024):
        for week in range(1, 8):
            rows.append(
                {
                    "season": season,
                    "week": week,
                    "game_type": "REG",
                    "gameday": f"{season}-09-{week + 7:02d}",
                    "home_team": "DET",
                    "away_team": "CHI",
                    "spread_line": 3.0,
                    "total_line": 45.0,
                }
            )
    return pl.DataFrame(rows)


def make_injuries(out_player="rb1", season=2024, week=5):
    return pl.DataFrame(
        [
            {
                "gsis_id": out_player,
                "season": season,
                "week": week,
                "game_type": "REG",
                "team": "DET",
                "report_status": "Out",
                "practice_status": "Did Not Participate In Practice",
            }
        ]
    )


def features(stats, **kwargs):
    return build_features(stats, make_opportunity(stats), make_schedules(), SCORING, **kwargs)


def row(frame, player_id, season, week):
    return frame.filter(
        (pl.col("player_id") == player_id) & (pl.col("season") == season) & (pl.col("week") == week)
    ).to_dicts()[0]


def test_points_are_scored_with_league_rules():
    stats = make_stats()
    points = stats.with_columns(points_expression(SCORING).alias("pts"))
    first = points.filter((pl.col("player_id") == "wr1") & (pl.col("week") == 1)).row(0, named=True)
    assert first["pts"] == pytest.approx(4 + 4.5)


def test_features_never_see_the_game_or_anything_after_it():
    clean = features(make_stats())
    leaked = features(make_stats(bump_from=(2024, 4)))
    columns = feature_columns(clean)

    before = row(clean, "wr1", 2024, 4)
    after = row(leaked, "wr1", 2024, 4)
    for column in columns:
        assert before[column] == pytest.approx(after[column], nan_ok=True), column

    # The change does show up in the next week's features.
    assert row(leaked, "wr1", 2024, 5)["pts_ewm_short"] > row(clean, "wr1", 2024, 5)["pts_ewm_short"]


def test_result_columns_are_not_features():
    columns = feature_columns(features(make_stats()))
    for column in ("pts", "xfp", "targets", "carries", "pts_over_xfp"):
        assert column not in columns


def test_rolling_features_start_empty_and_count_games():
    frame = features(make_stats())
    first = row(frame, "wr1", 2023, 1)
    assert first["career_games"] == 0
    assert first["pts_ewm_long"] is None
    later = row(frame, "wr1", 2024, 2)
    assert later["career_games"] == 7
    assert later["season_games"] == 1
    assert later["prev_season_ppg"] == pytest.approx(
        row(frame, "wr1", 2023, 1)["pts"] * 0 + frame.filter(
            (pl.col("player_id") == "wr1") & (pl.col("season") == 2023)
        )["pts"].mean()
    )


def test_vegas_lines_split_into_team_totals():
    frame = features(make_stats())
    home = row(frame, "wr1", 2024, 3)
    away = row(frame, "rb2", 2024, 3)
    assert home["implied_total"] == pytest.approx(24.0)
    assert away["implied_total"] == pytest.approx(21.0)
    assert home["is_home"] == 1 and away["is_home"] == 0


def test_teammate_ruled_out_leaves_vacated_share():
    stats = make_stats().filter(
        ~((pl.col("player_id") == "rb1") & (pl.col("season") == 2024) & (pl.col("week") == 5))
    )
    frame = features(stats, injuries=make_injuries())
    receiver = row(frame, "wr1", 2024, 5)
    assert receiver["vacated_carry_share"] == pytest.approx(0.4, abs=0.01)
    assert row(frame, "wr1", 2024, 4)["vacated_carry_share"] == 0


def test_upcoming_week_rows_have_features_but_no_result():
    frame = features(make_stats(), upcoming=(2024, 7))
    upcoming = row(frame, "wr1", 2024, 7)
    assert upcoming["pts"] is None
    assert upcoming["career_games"] == 12
    assert upcoming["implied_total"] == pytest.approx(24.0)
    assert upcoming["opp_allowed_ratio"] is not None


# ------------------------------------------------------------ backtest


def graded_frame():
    # The model is right about half the gap between the experts and the
    # result, so a moderate weight should win.
    rows = []
    for season in (2020, 2021, 2022):
        for i in range(40):
            expert = 10.0 + (i % 7)
            actual = expert + (3.0 if i % 2 else -3.0)
            model = expert + (6.0 if i % 2 else -6.0)
            rows.append(
                {"position": "RB", "season": season, "week": 1 + i % 5, "pts": actual,
                 "espn_sleeper_blend": expert, "model": model}
            )
    return pl.DataFrame(rows).with_columns(pl.col("season").cast(pl.Int32))


def test_best_weight_and_edge_slope():
    frame = graded_frame()
    assert backtest.best_weight(frame) == 0.4
    assert backtest.edge_slopes(frame)["RB"] == pytest.approx(0.5)


def test_weights_are_chosen_from_earlier_seasons_only():
    weights = backtest.walk_forward_weights(graded_frame(), [2020, 2021, 2022])
    rb = {r["season"]: r["weight"] for r in weights.filter(pl.col("position") == "RB").to_dicts()}
    assert rb[2020] == 0.0
    assert rb[2021] == 0.4


def test_promotion_needs_better_error_and_rank_order():
    table = pl.DataFrame(
        [
            {"position": "RB", "method": "espn_sleeper_blend", "mae": 5.0, "spearman": 0.50},
            {"position": "RB", "method": "blend_with_model", "mae": 4.9, "spearman": 0.51},
            {"position": "WR", "method": "espn_sleeper_blend", "mae": 5.0, "spearman": 0.50},
            {"position": "WR", "method": "blend_with_model", "mae": 4.9, "spearman": 0.49},
        ]
    )
    decisions = backtest.promotion(table)
    assert decisions["RB"] is True
    assert decisions["WR"] is False
    assert decisions["QB"] is False


def test_grade_reports_error_and_weekly_rank_correlation():
    frame = pl.DataFrame(
        {"season": [2024] * 10, "week": [1] * 10, "pts": [float(i) for i in range(10)],
         "guess": [float(i) + 1 for i in range(10)]}
    )
    result = backtest.grade(frame, "guess")
    assert result["mae"] == 1.0
    assert result["spearman"] == pytest.approx(1.0)
