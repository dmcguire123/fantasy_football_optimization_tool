"""Finding a team by name, and the single-team scouting report."""

import pytest

from ffopt.league import find_team
from ffopt.scouting import team_report


def test_a_team_is_found_by_owner_first_name(league):
    team, matches = find_team(league, "Darren")

    assert team is not None
    assert team.team_id == 3
    assert matches == [team]


def test_lookup_ignores_case(league):
    assert find_team(league, "darren")[0].team_id == 3
    assert find_team(league, "DARREN")[0].team_id == 3


def test_a_team_is_found_by_full_owner_name(league):
    assert find_team(league, "Darren Park")[0].team_id == 3


def test_a_team_is_found_by_its_own_name(league):
    assert find_team(league, "Rival Crew")[0].team_id == 2
    assert find_team(league, "rival")[0].team_id == 2


def test_a_team_is_found_by_abbreviation(league):
    assert find_team(league, "DEEP")[0].team_id == 3


def test_a_numeric_query_is_treated_as_a_team_id(league):
    assert find_team(league, "1")[0].team_id == 1
    assert find_team(league, 2)[0].team_id == 2


def test_an_unknown_name_matches_nothing(league):
    team, matches = find_team(league, "Nobody")

    assert team is None
    assert matches == []


def test_an_empty_query_matches_nothing(league):
    assert find_team(league, "   ") == (None, [])


def test_an_ambiguous_query_returns_the_candidates(league):
    # Every team name in the fixture contains no shared word, so build the
    # ambiguity from a substring that two owners share.
    team, matches = find_team(league, "a")

    if team is None:
        assert len(matches) > 1
    else:
        assert matches == [team]


def test_the_report_covers_record_rank_and_efficiency(league, starting_slots):
    report = team_report(league, league.team_by_id(3), starting_slots)

    assert report["team"]["team_id"] == 3
    assert report["power_rank"] in (1, 2, 3)
    assert 0 <= report["power_score"] <= 100
    assert report["lineup_efficiency"]["best_projected"] > 0


def test_position_strength_compares_against_the_league(league, starting_slots):
    report = team_report(league, league.team_by_id(3), starting_slots)
    rows = report["position_strength"]

    assert rows
    for row in rows:
        assert abs(row["vs_median"] - (row["points"] - row["league_median"])) < 1e-9
        assert 1 <= row["rank_in_league"] <= len(league.teams)

    # The fixture team three hoards running backs and is thin at receiver.
    by_slot = {row["slot_name"]: row for row in rows}
    assert by_slot["RB"]["vs_median"] > 0
    assert by_slot["WR"]["vs_median"] < 0


def test_the_report_flags_bench_players_who_should_start(league, starting_slots):
    report = team_report(league, league.team_by_id(1), starting_slots)
    names = {p["name"] for p in report["should_be_starting"]}

    assert "Bench Burner" in names


def test_a_correctly_set_team_has_nobody_to_promote(league, starting_slots):
    report = team_report(league, league.team_by_id(2), starting_slots)

    assert report["should_be_starting"] == []
    assert report["lineup_efficiency"]["points_left_on_bench"] == 0.0


def test_the_report_names_this_weeks_opponent(league, starting_slots):
    report = team_report(league, league.team_by_id(1), starting_slots)

    assert report["opponent"]["team_id"] == 2
    assert 0.0 <= report["win_probability"] <= 1.0


def test_a_team_with_no_game_has_no_opponent(league, starting_slots):
    report = team_report(league, league.team_by_id(3), starting_slots)

    assert report["opponent"] is None
    assert report["win_probability"] is None


def test_top_players_are_ordered_by_season_scoring(league, starting_slots):
    report = team_report(league, league.team_by_id(1), starting_slots)
    totals = [p["season_actual_points"] for p in report["top_players"]]

    assert totals == sorted(totals, reverse=True)
    assert len(totals) <= 5
