"""
The season dashboard: week-by-week projections, matchups, and outlooks.
"""

import pytest

from ffopt import dashboard
from ffopt.models import Player
from ffopt.projections.service import ProjectionSet


WEEK = 5


def projection_set():
    projection_set = ProjectionSet(2025, WEEK)
    for week, points in ((6, 20.0), (7, 10.0)):
        table = projection_set.by_week.setdefault(week, {})
        projection_set.add(table, "espn", "101", "Ace Passer", "QB", "KC", points)
        projection_set.add(table, "sleeper", "101", "Ace Passer", "QB", "KC", points + 2)
        projection_set.add(table, "model", "101", "Ace Passer", "QB", "KC", 99.0)
    return projection_set


def projector(league_byes=None):
    return dashboard.WeekProjector(
        projection_set(),
        WEEK,
        league_byes or {"GB": 5, "KC": 7},
        {"KC": {5: ("vs", "MIN"), 6: ("vs", "GB")}},
    )


def ace():
    return Player(player_id=101, name="Ace Passer", position="QB", pro_team="KC",
                  projected_points=21.0, source_points={"espn": 21.0, "sleeper": 22.0})


def test_this_week_uses_the_players_current_projection():
    player = ace()
    assert projector().points(player, WEEK) == 21.0
    assert projector().sources(player, WEEK) == {"espn": 21.0, "sleeper": 22.0}


def test_future_weeks_blend_the_experts_but_not_the_model():
    assert projector().points(ace(), 6) == pytest.approx(21.0)
    assert projector().sources(ace(), 6)["model"] == 99.0


def test_bye_week_scores_zero_and_shows_as_bye():
    assert projector().points(ace(), 7) == 0.0
    assert projector().nfl_game(ace(), 7) == "BYE"
    assert projector().nfl_game(ace(), 6) == "vs GB"


def test_matchup_for_a_future_week_uses_that_weeks_projections(service):
    league = service.load_league()
    team = service.my_team()
    result = dashboard.matchup(league, team, projector(), 6)

    assert result["week"] == 6 and not result["is_current"]
    ace_line = [p for p in result["mine"]["starters"] if p.get("name") == "Ace Passer"][0]
    assert ace_line["projection"] == pytest.approx(21.0)
    assert ace_line["game"] == "vs GB"
    assert all(line["slot"] in ("BE", "IR") for line in result["mine"]["bench"])


def test_season_schedule_lists_results_then_projections(service):
    league = service.load_league()
    rows = dashboard.season_schedule(league, service.my_team(), projector())
    assert rows
    for row in rows:
        if row["played"]:
            assert "my_score" in row
        elif row["opponent"]:
            assert 0.0 <= row["win_probability"] <= 1.0


def test_player_outlook_lists_every_remaining_week():
    outlook = dashboard.player_outlook(ace(), projector(), 7, owner="My Squad")
    assert [r["week"] for r in outlook["future"]] == [5, 6, 7]
    assert outlook["future"][2]["on_bye"] is True
    assert outlook["owner"] == "My Squad"
