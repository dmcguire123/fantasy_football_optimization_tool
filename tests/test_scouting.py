"""League-wide analysis: rankings, opponent reports, surplus, trades."""

from ffopt.scouting import (
    lineup_efficiency,
    opponent_report,
    positional_matchup,
    positional_surplus,
    power_rankings,
    trade_targets,
    win_probability,
)


def test_win_probability_is_a_calibrated_coin_flip_at_zero_margin():
    assert abs(win_probability(100.0, 100.0) - 0.5) < 1e-9


def test_win_probability_moves_the_right_way():
    assert win_probability(130.0, 100.0) > 0.7
    assert win_probability(100.0, 130.0) < 0.3
    assert 0.0 < win_probability(500.0, 10.0) <= 1.0


def test_efficiency_measures_points_left_on_the_bench(league, starting_slots):
    mine = lineup_efficiency(league.team_by_id(1), starting_slots)
    rival = lineup_efficiency(league.team_by_id(2), starting_slots)

    assert mine["points_left_on_bench"] > 0
    assert mine["efficiency"] < 1.0
    assert rival["points_left_on_bench"] == 0.0
    assert rival["efficiency"] == 1.0


def test_power_rankings_cover_every_team_and_are_ordered(league, starting_slots):
    rankings = power_rankings(league, starting_slots)

    assert len(rankings) == len(league.teams)
    assert [r["power_rank"] for r in rankings] == [1, 2, 3]

    scores = [r["power_score"] for r in rankings]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 100 for s in scores)


def test_power_rankings_reward_the_strongest_roster(league, starting_slots):
    rankings = power_rankings(league, starting_slots)
    top = rankings[0]["team"]

    # Team 2 leads in both season scoring and current roster strength.
    assert top["team_id"] == 2


def test_opponent_report_identifies_this_weeks_matchup(league, starting_slots):
    team = league.team_by_id(1)
    report = opponent_report(league, team, starting_slots)

    assert report["has_opponent"]
    assert report["opponent"]["team_id"] == 2
    assert report["my_projected_best"] > report["my_projected_current"]
    assert 0.0 <= report["win_probability_if_optimal"] <= 1.0


def test_fixing_your_lineup_improves_your_odds(league, starting_slots):
    team = league.team_by_id(1)
    report = opponent_report(league, team, starting_slots)

    assert report["win_probability_if_optimal"] > report["win_probability_as_set"]


def test_report_flags_opponent_starters_who_may_not_play(league, starting_slots):
    team = league.team_by_id(1)
    report = opponent_report(league, team, starting_slots)

    assert isinstance(report["their_risks"], list)
    assert report["their_lineup_efficiency"]["points_left_on_bench"] == 0.0


def test_a_team_with_no_game_gets_a_clear_message(league, starting_slots):
    team = league.team_by_id(3)
    report = opponent_report(league, team, starting_slots)

    assert report["has_opponent"] is False
    assert "No opponent" in report["message"]


def test_positional_edges_cover_each_starting_slot(league, starting_slots):
    rows = positional_matchup(
        league.team_by_id(1), league.team_by_id(2), starting_slots
    )
    slot_names = {row["slot_name"] for row in rows}

    assert {"QB", "RB", "WR", "TE", "FLEX", "K", "D/ST"} <= slot_names
    for row in rows:
        assert abs(row["edge"] - (row["my_points"] - row["their_points"])) < 1e-9


def test_surplus_shows_the_running_back_hoarder(league, starting_slots):
    rows = positional_surplus(league, starting_slots)
    deep_team = next(r for r in rows if r["team"]["team_id"] == 3)

    assert deep_team["positions"]["RB"]["surplus"] > 1
    assert deep_team["positions"]["WR"]["surplus"] < deep_team["positions"]["RB"]["surplus"]


def test_trade_targets_point_at_the_team_with_spare_backs(league, starting_slots):
    team = league.team_by_id(1)
    targets = trade_targets(league, team, starting_slots)

    assert targets, "expected at least one trade partner"
    team_ids = {entry["team"]["team_id"] for entry in targets}
    assert team.team_id not in team_ids

    for entry in targets:
        for match in entry["players"]:
            assert match["weekly_gain_for_me"] > 0


def test_trade_entries_flag_players_the_other_team_can_spare(league, starting_slots):
    team = league.team_by_id(1)
    targets = trade_targets(league, team, starting_slots)

    deep_team = next(
        (entry for entry in targets if entry["team"]["team_id"] == 3), None
    )
    assert deep_team is not None, "the running back hoarder should be a partner"
    assert "RB" in deep_team["their_surplus_positions"]
    assert any(match["they_can_spare"] for match in deep_team["players"])


def test_trade_entries_suggest_what_to_send_back(league, starting_slots):
    team = league.team_by_id(1)
    targets = trade_targets(league, team, starting_slots)

    offered = [
        offer
        for entry in targets
        for offer in entry["could_offer"]
    ]
    assert offered, "expected at least one player to offer in return"
    for offer in offered:
        assert offer["weekly_gain_for_them"] > 0


def test_trade_targets_are_ranked_by_how_much_they_help(league, starting_slots):
    team = league.team_by_id(1)
    targets = trade_targets(league, team, starting_slots)

    best_per_team = [
        max(p["weekly_gain_for_me"] for p in entry["players"]) for entry in targets
    ]
    assert best_per_team == sorted(best_per_team, reverse=True)
