"""
Projection trends and the model's rest-of-season rows.
"""

import time

import polars as pl
import pytest

from ffopt.projections import store
from ffopt.projections.model import future_games, future_rows
from ffopt.projections.trends import TrendData, _load_archive, ros_movers, weekly_movers
from ffopt.projections.features import build_features

from test_model_features import SCORING, make_opportunity, make_schedules, make_stats


# ---------------------------------------------------------------- movers


def trend_data():
    data = TrendData(2026, 3)
    data.note_player("1", "Role Change", "RB", "DET")
    data.note_player("2", "Steady Eddie", "WR", "DET")
    data.note_player("3", "Benched Guy", "QB", "CHI")
    for week, points in ((1, 4.0), (2, 5.0), (3, 13.0)):
        data._week("1", week).update(espn=points, sleeper=points)
    for week in (1, 2, 3):
        data._week("2", week).update(espn=12.0, sleeper=12.5)
    data._week("3", 1)["espn"] = 18.0
    data._week("3", 3)["consensus"] = 2.0
    return data


def test_weekly_movers_compare_this_week_to_earlier_weeks():
    movers = weekly_movers(trend_data())
    assert [m["name"] for m in movers["risers"]] == ["Role Change"]
    assert movers["risers"][0]["change"] == pytest.approx(13.0 - 4.5)
    assert [m["name"] for m in movers["fallers"]] == ["Benched Guy"]
    assert movers["fallers"][0]["change"] == pytest.approx(-16.0)


def test_weekly_movers_can_be_limited_to_some_players():
    movers = weekly_movers(trend_data(), only={"2"})
    assert movers == {"risers": [], "fallers": []}


def test_ros_movers_need_a_pull_from_a_week_before():
    data = trend_data()
    data.ros["1"] = {"consensus": [("2026-09-10", 100.0), ("2026-09-20", 130.0)]}
    data.ros["2"] = {"consensus": [("2026-09-18", 150.0), ("2026-09-20", 100.0)]}
    movers = ros_movers(data, days=5)
    assert [m["name"] for m in movers["risers"]] == ["Role Change"]
    assert movers["risers"][0]["since"] == "2026-09-10"
    # Steady Eddie's drop is only two days old, so it is not compared yet.
    assert movers["fallers"] == []
    assert movers["days_of_history"] == 2


def test_player_history_blends_experts_when_no_blend_was_archived():
    history = trend_data().player("1")
    assert history["name"] == "Role Change"
    assert history["weeks"][0]["experts_blend"] == pytest.approx(4.0)


# ------------------------------------------------------------ archive


def test_archive_gives_latest_weekly_pull_and_one_ros_point_per_day(tmp_path):
    connection = store.open_db(tmp_path / "p.db")
    day_one = time.mktime((2026, 9, 20, 9, 0, 0, 0, 0, -1))
    day_two = time.mktime((2026, 9, 21, 9, 0, 0, 0, 0, -1))
    row = {"source_id": "1", "espn_id": "1", "name": "Role Change", "position": "RB",
           "team": "DET", "stats": {}}
    store.save_pull(connection, 2026, 3, "model", [{**row, "points": 9.0}], day_one)
    store.save_pull(connection, 2026, 3, "model", [{**row, "points": 11.0}], day_two)
    store.save_pull(connection, 2026, 3, "consensus_ros", [{**row, "points": 120.0}], day_one)
    store.save_pull(connection, 2026, 3, "consensus_ros", [{**row, "points": 110.0}], day_one + 60)
    store.save_pull(connection, 2026, 3, "consensus_ros", [{**row, "points": 140.0}], day_two)

    data = TrendData(2026, 3)
    _load_archive(data, connection, 2026)
    assert data.weekly["1"][3]["model"] == 11.0
    assert data.ros["1"]["consensus"] == [("2026-09-20", 110.0), ("2026-09-21", 140.0)]


# ------------------------------------------------ model rest of season


def test_future_games_fill_missing_lines_with_team_averages():
    schedules = make_schedules().with_columns(
        pl.when(pl.col("week") >= 6).then(None).otherwise(pl.col("total_line")).alias("total_line")
    )
    games = future_games(schedules, 2024, 5, 7)
    det = games.filter(pl.col("team") == "DET").sort("week")
    assert det["week"].to_list() == [5, 6, 7]
    assert det["implied_total"].to_list() == pytest.approx([24.0, 24.0, 24.0])
    assert det["opponent_team"].to_list() == ["CHI"] * 3


def test_future_rows_pair_current_player_features_with_each_future_game():
    stats = make_stats()
    features = build_features(stats, make_opportunity(stats), make_schedules(), SCORING,
                              upcoming=(2024, 7))
    current = features.filter((pl.col("season") == 2024) & (pl.col("week") == 7))
    schedules = pl.concat([
        make_schedules(),
        make_schedules().filter(pl.col("season") == 2024).with_columns(pl.col("week") + 7),
    ])
    later = future_rows(features, current, schedules, 2024, 7, 9)

    wr = later.filter(pl.col("player_id") == "wr1").sort("week")
    assert wr["week"].to_list() == [8, 9]
    now = current.filter(pl.col("player_id") == "wr1").row(0, named=True)
    for column in ("pts_ewm_long", "career_games", "targets_ewm_long"):
        assert wr[column].to_list() == [now[column]] * 2
    assert wr.columns == current.columns
