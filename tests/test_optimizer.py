"""The lineup solver: correctness, FLEX handling, and the move diff."""

import itertools

from conftest import ELIGIBLE

from ffopt import constants as C
from ffopt.models import Player
from ffopt.optimizer import (
    best_possible_total,
    build_moves,
    optimize_team,
    season_projection,
    solve_lineup,
    week_projection,
)


def make(player_id, position, points, slot=C.BENCH_SLOT, **kwargs):
    return Player(
        player_id=player_id,
        name=f"{position}{player_id}",
        position=position,
        pro_team="KC",
        eligible_slots=ELIGIBLE[position],
        lineup_slot=slot,
        projected_points=points,
        **kwargs,
    )


def brute_force_best(players, slots):
    best = 0.0
    for combo in itertools.permutations(range(len(players)), len(slots)):
        total = 0.0
        for slot, index in zip(slots, combo):
            player = players[index]
            if slot not in player.startable_slots():
                break
            total += player.effective_projection
        else:
            best = max(best, total)
    return best


def test_every_slot_gets_filled_and_no_player_is_used_twice(league, starting_slots):
    team = league.team_by_id(1)
    roster = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]

    assignments, bench = solve_lineup(roster, starting_slots)
    used = [p for _, p in assignments if p]

    assert len(assignments) == len(starting_slots)
    assert all(player is not None for _, player in assignments)
    assert len({p.player_id for p in used}) == len(used)
    assert len(used) + len(bench) == len(roster)


def test_players_only_land_in_slots_they_are_eligible_for(league, starting_slots):
    team = league.team_by_id(1)
    assignments, _ = solve_lineup(team.roster, starting_slots)

    for slot_id, player in assignments:
        assert slot_id in player.startable_slots(), f"{player.name} cannot play {slot_id}"


def test_flex_goes_to_the_best_leftover_skill_player():
    players = [
        make(1, "QB", 20.0),
        make(2, "RB", 18.0),
        make(3, "RB", 17.0),
        make(4, "RB", 16.0),
        make(5, "WR", 9.0),
        make(6, "WR", 8.0),
        make(7, "WR", 7.0),
        make(8, "TE", 6.0),
    ]
    slots = [0, 2, 2, 4, 4, 6, 23]

    assignments, _ = solve_lineup(players, slots)
    flex = next(p for slot, p in assignments if slot == 23)

    # The third running back outscores every spare receiver, so it takes FLEX.
    assert flex.player_id == 4


def test_solver_matches_brute_force_on_a_tricky_roster():
    players = [
        make(1, "QB", 22.0),
        make(2, "RB", 14.0),
        make(3, "WR", 13.5),
        make(4, "TE", 13.0),
        make(5, "WR", 12.5),
        make(6, "RB", 12.0),
    ]
    slots = [0, 2, 4, 23]

    assignments, _ = solve_lineup(players, slots)
    total = sum(p.effective_projection for _, p in assignments if p)

    assert abs(total - brute_force_best(players, slots)) < 1e-9


def test_a_bye_week_starter_gets_benched(league, starting_slots):
    team = league.team_by_id(1)
    result = optimize_team(team, starting_slots)

    started_ids = {p.player_id for _, p in result.assignments if p}
    assert 104 not in started_ids, "player on bye should not be started"
    assert 112 not in started_ids, "player ruled out should not be started"


def test_optimizer_finds_the_benched_star(league, starting_slots):
    team = league.team_by_id(1)
    result = optimize_team(team, starting_slots)

    started_ids = {p.player_id for _, p in result.assignments if p}
    assert 102 in started_ids, "the 18-point bench back should be starting"
    assert result.points_gained > 0
    assert result.total >= result.current_total


def test_moves_describe_a_complete_swap(league, starting_slots):
    team = league.team_by_id(1)
    result = optimize_team(team, starting_slots)

    moved_in = [m for m in result.moves if m["to_slot"] != C.BENCH_SLOT]
    moved_out = [m for m in result.moves if m["to_slot"] == C.BENCH_SLOT]

    assert moved_in, "expected at least one player moving into the lineup"
    assert len(moved_in) == len(moved_out), "every promotion needs a matching demotion"

    for move in result.moves:
        assert move["from_slot"] != move["to_slot"]
        assert move["player_name"]


def test_injured_reserve_players_are_left_alone(league, starting_slots):
    team = league.team_by_id(1)
    result = optimize_team(team, starting_slots)

    assert all(m["player_id"] != 114 for m in result.moves)
    assert all(m["from_slot"] != C.IR_SLOT for m in result.moves)


def test_an_already_optimal_lineup_produces_no_moves(league, starting_slots):
    team = league.team_by_id(2)
    result = optimize_team(team, starting_slots)

    assert result.moves == []
    assert result.to_dict()["is_already_optimal"] is True


def test_locked_players_keep_their_slot(league, starting_slots):
    team = league.team_by_id(1)
    locked_starter = team.player_by_id(103)

    result = optimize_team(team, starting_slots, locked_player_ids={103})

    assert all(m["player_id"] != 103 for m in result.moves)
    assigned = {p.player_id for _, p in result.assignments if p}
    assert locked_starter.player_id in assigned


def test_season_horizon_uses_season_projections():
    weekly_star = make(1, "RB", 20.0, season_projected_points=40.0)
    season_star = make(2, "RB", 5.0, season_projected_points=300.0)

    by_week, _ = solve_lineup([weekly_star, season_star], [2], week_projection)
    by_season, _ = solve_lineup([weekly_star, season_star], [2], season_projection)

    assert by_week[0][1].player_id == 1
    assert by_season[0][1].player_id == 2


def test_best_possible_total_ignores_how_the_lineup_is_currently_set(league, starting_slots):
    team = league.team_by_id(1)
    best = best_possible_total(team.roster, starting_slots)

    assert best > team.projected_starting_points


def test_build_moves_returns_nothing_when_slots_already_match():
    player = make(1, "QB", 20.0, slot=0)
    assignments = [(0, player)]

    assert build_moves(assignments, []) == []


def test_empty_slot_list_benches_everyone():
    assignments, bench = solve_lineup([make(1, "QB", 20.0)], [])
    assert assignments == []
    assert len(bench) == 1
