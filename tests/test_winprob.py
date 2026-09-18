"""Win probability and the win-probability lineup objective."""

from conftest import ELIGIBLE

from ffopt import constants as C
from ffopt.models import Player, Team
from ffopt.optimizer import best_lineup_win_probability, optimize_team
from ffopt.winprob import (
    WEEKLY_SCORE_STDDEV,
    lineup_distribution,
    player_stddev,
    win_probability,
)


def make(player_id, position, points, slot=C.BENCH_SLOT):
    return Player(
        player_id=player_id,
        name=f"{position}{player_id}",
        position=position,
        pro_team="KC",
        eligible_slots=ELIGIBLE[position],
        lineup_slot=slot,
        projected_points=points,
    )


def team_of(players):
    return Team(team_id=1, name="Me", abbrev="ME", roster=players)


def test_even_matchup_is_a_coin_flip():
    assert abs(win_probability(100, 100) - 0.5) < 1e-9


def test_default_spread_matches_league_typical():
    explicit = win_probability(110, 100, WEEKLY_SCORE_STDDEV, WEEKLY_SCORE_STDDEV)
    assert abs(win_probability(110, 100) - explicit) < 1e-9


def test_larger_spread_pulls_toward_a_coin_flip():
    assert win_probability(110, 100, 10, 10) > win_probability(110, 100, 40, 40)


def test_zero_projection_player_has_no_spread():
    assert player_stddev(make(1, "WR", 0.0)) == 0.0


def test_receivers_are_riskier_than_quarterbacks_at_equal_points():
    assert player_stddev(make(1, "WR", 15.0)) > player_stddev(make(2, "QB", 15.0))


def test_lineup_variance_adds_across_players():
    players = [make(1, "WR", 10.0), make(2, "WR", 10.0)]
    mean, stddev = lineup_distribution(players)
    single = player_stddev(players[0])
    assert mean == 20.0
    assert abs(stddev - (2 * single**2) ** 0.5) < 1e-9


def test_underdog_takes_the_boom_player_and_favorite_the_steady_one():
    slots = [23]
    steady = make(1, "RB", 14.0, slot=23)
    boom = make(2, "WR", 13.0)
    team = team_of([steady, boom])

    # Against a much stronger opponent the extra spread is worth more than
    # the one point given up.
    underdog = optimize_team(team, slots, objective="win", opponent=(40.0, 5.0))
    assert underdog.assignments[0][1].player_id == 2

    # Against a much weaker one, the steadier player wins out.
    favorite = optimize_team(team, slots, objective="win", opponent=(0.0, 5.0))
    assert favorite.assignments[0][1].player_id == 1


def test_points_objective_is_unchanged_by_an_opponent():
    slots = [23]
    team = team_of([make(1, "RB", 14.0, slot=23), make(2, "WR", 13.0)])
    result = optimize_team(team, slots, objective="points", opponent=(30.0, 10.0))
    assert result.assignments[0][1].player_id == 1
    assert result.win_probability is None
    assert result.objective == "points"


def test_win_objective_without_an_opponent_falls_back_to_points():
    slots = [23]
    team = team_of([make(1, "RB", 14.0, slot=23), make(2, "WR", 13.0)])
    result = optimize_team(team, slots, objective="win")
    assert result.assignments[0][1].player_id == 1
    assert result.objective == "points"


def test_win_lineup_is_never_worse_than_points_lineup():
    slots = [2, 4, 23]
    roster = [
        make(1, "RB", 15.0, slot=2),
        make(2, "WR", 12.0, slot=4),
        make(3, "RB", 9.0, slot=23),
        make(4, "WR", 8.5),
        make(5, "TE", 8.0),
    ]
    team = team_of(roster)
    opponent = (45.0, 20.0)
    points = optimize_team(team, slots)
    win = optimize_team(team, slots, objective="win", opponent=opponent)
    points_chance = best_lineup_win_probability(roster, slots, opponent)
    assert win.win_probability >= points_chance - 1e-9
    assert all(p is not None for _, p in win.assignments)
    assert points.total >= win.total - 1e-9
