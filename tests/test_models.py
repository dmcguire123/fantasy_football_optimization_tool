"""Parsing ESPN's nested player and team records."""

from conftest import WEEK, make_player, roster_entry

from ffopt import constants as C
from ffopt.models import player_from_raw, player_from_roster_entry


def test_player_picks_the_right_week_and_source():
    raw = make_player(1, "Test Back", "RB", 12.5)
    player = player_from_raw(raw, WEEK, 2025)

    assert player.name == "Test Back"
    assert player.position == "RB"
    assert player.projected_points == 12.5
    assert player.actual_points == 0.0
    assert player.season_projected_points == 12.5 * 14


def test_bye_week_zeroes_the_effective_projection():
    raw = make_player(2, "Bye Guy", "WR", 14.0, pro_team_id=9)
    player = player_from_raw(raw, WEEK, 2025, bye_weeks={9: WEEK})

    assert player.on_bye
    assert player.projected_points == 14.0
    assert player.effective_projection == 0.0
    assert not player.is_playable


def test_players_ruled_out_are_not_playable():
    out = player_from_raw(make_player(3, "Hurt", "RB", 10.0, injury="OUT"), WEEK, 2025)
    questionable = player_from_raw(
        make_player(4, "Maybe", "RB", 10.0, injury="QUESTIONABLE"), WEEK, 2025
    )

    assert not out.is_playable
    assert out.effective_projection == 0.0
    assert questionable.is_playable
    assert questionable.effective_projection == 10.0


def test_roster_entry_carries_the_lineup_slot():
    entry = roster_entry(make_player(5, "Starter", "QB", 20.0), 0, 7)
    player = player_from_roster_entry(entry, WEEK, 2025)

    assert player.lineup_slot == 0
    assert player.lineup_slot_name == "QB"
    assert player.is_starting
    assert player.fantasy_team_id == 7


def test_bench_and_ir_players_are_not_starting():
    bench = player_from_roster_entry(
        roster_entry(make_player(6, "Benched", "RB", 9.0), C.BENCH_SLOT, 1), WEEK, 2025
    )
    injured = player_from_roster_entry(
        roster_entry(make_player(7, "Shelved", "RB", 0.0), C.IR_SLOT, 1), WEEK, 2025
    )

    assert not bench.is_starting
    assert not injured.is_starting


def test_startable_slots_exclude_bench_and_ir():
    player = player_from_raw(make_player(8, "Flexible", "WR", 11.0), WEEK, 2025)
    slots = player.startable_slots()

    assert C.BENCH_SLOT not in slots
    assert C.IR_SLOT not in slots
    assert 4 in slots and 23 in slots


def test_team_projection_counts_only_starters(league):
    team = league.team_by_id(1)
    starters = team.starters

    assert all(p.is_starting for p in starters)
    expected = sum(p.effective_projection for p in starters)
    assert abs(team.projected_starting_points - expected) < 1e-9
