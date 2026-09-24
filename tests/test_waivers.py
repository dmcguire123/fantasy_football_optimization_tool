"""Waiver evaluation, bid sizing, and claim payloads."""

import json

import pytest

from ffopt import constants as C
from ffopt.waivers import (
    build_claim,
    drop_candidates,
    evaluate_pickup,
    recommend_pickups,
    suggest_faab_bid,
)


def test_a_pickup_is_worth_what_it_adds_to_the_lineup(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(limit=50)
    gem = next(p for p in pool if p.player_id == 401)

    evaluation = evaluate_pickup(team, gem, starting_slots, roster_limit=0)

    assert evaluation.weekly_gain > 0
    assert evaluation.player.player_id == 401


def test_a_player_worse_than_everyone_adds_nothing(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(limit=50)
    sleeper = next(p for p in pool if p.player_id == 404)

    evaluation = evaluate_pickup(team, sleeper, starting_slots, roster_limit=0)

    assert evaluation.weekly_gain == 0.0


def test_a_full_roster_forces_a_drop(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(limit=50)
    gem = next(p for p in pool if p.player_id == 401)

    roster_size = len([p for p in team.roster if p.lineup_slot != C.IR_SLOT])
    evaluation = evaluate_pickup(team, gem, starting_slots, roster_limit=roster_size)

    assert evaluation.needs_drop
    assert evaluation.drop_player is not None
    assert evaluation.drop_player.lineup_slot != C.IR_SLOT


def test_the_suggested_drop_is_someone_cheap(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(limit=50)
    gem = next(p for p in pool if p.player_id == 401)

    roster_size = len([p for p in team.roster if p.lineup_slot != C.IR_SLOT])
    evaluation = evaluate_pickup(team, gem, starting_slots, roster_limit=roster_size)

    # The best player on the roster should never be the recommended drop.
    assert evaluation.drop_player.player_id != 101


def test_recommendations_are_ranked_and_capped(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(limit=50)

    recommendations = recommend_pickups(
        team, pool, starting_slots, faab_remaining=75.0, limit=3
    )

    assert len(recommendations) <= 3
    gains = [r.weekly_gain for r in recommendations]
    assert gains == sorted(gains, reverse=True)


def test_position_filter_narrows_the_pool(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(limit=50)

    recommendations = recommend_pickups(
        team, pool, starting_slots, positions=["QB"], limit=10
    )

    assert all(r.player.position == "QB" for r in recommendations)


def test_bids_scale_with_value_and_stay_inside_the_budget():
    assert suggest_faab_bid(0.0, 100.0) == 0.0
    assert suggest_faab_bid(5.0, 0.0) == 0.0

    small = suggest_faab_bid(1.0, 100.0)
    large = suggest_faab_bid(9.0, 100.0)

    assert 0 < small < large
    assert large <= 50.0


def test_a_tiny_gain_still_bids_at_least_a_dollar():
    assert suggest_faab_bid(0.2, 100.0) == 1.0


def test_no_single_bid_exceeds_half_the_remaining_budget():
    assert suggest_faab_bid(500.0, 40.0) <= 20.0


def test_drop_candidates_are_ordered_cheapest_first(league, starting_slots):
    team = league.team_by_id(1)
    candidates = drop_candidates(team, starting_slots, limit=5)

    costs = [row["season_cost"] for row in candidates]
    assert costs == sorted(costs)
    assert all(row["player"]["lineup_slot_name"] != "IR" for row in candidates)


def test_waiver_claim_payload_has_both_sides_and_a_bid():
    payload = build_claim(
        team_id=1, member_id="{ME}", week=5,
        add_player_id=401, drop_player_id=113, bid_amount=12,
    )

    assert payload["type"] == "WAIVER"
    assert payload["teamId"] == 1
    assert payload["bidAmount"] == 12.0
    assert payload["scoringPeriodId"] == 5

    kinds = {item["type"]: item for item in payload["items"]}
    assert kinds["ADD"]["playerId"] == 401
    assert kinds["ADD"]["toTeamId"] == 1
    assert kinds["DROP"]["playerId"] == 113
    assert kinds["DROP"]["fromTeamId"] == 1


def test_free_agent_add_is_a_different_type_and_carries_no_bid():
    payload = build_claim(
        team_id=1, member_id="{ME}", week=5, add_player_id=401, is_waiver=False
    )

    assert payload["type"] == "FREEAGENT"
    assert "bidAmount" not in payload
    assert len(payload["items"]) == 1


def test_claim_payload_is_json_serializable():
    payload = build_claim(
        team_id=1, member_id="{ME}", week=5, add_player_id=401, drop_player_id=113
    )
    assert json.loads(json.dumps(payload))["items"][0]["playerId"] == 401


# ------------------------------------------- streams and season cost

from ffopt.models import Player, Team
from ffopt.optimizer import season_projection


def dst(player_id, name, week, ros, slot=C.BENCH_SLOT):
    return Player(player_id=player_id, name=name, position="D/ST", pro_team="DEN",
                  eligible_slots=[16, 20, 21], lineup_slot=slot, projected_points=week,
                  ros_points=ros, ros_games=14)


def wr(player_id, name, week, ros, slot=C.BENCH_SLOT):
    return Player(player_id=player_id, name=name, position="WR", pro_team="DET",
                  eligible_slots=[4, 23, 20, 21], lineup_slot=slot, projected_points=week,
                  ros_points=ros, ros_games=14)


# One WR slot and one D/ST slot, a bench WR, and a full roster: the Broncos
# case. The Giants are better this week and worse over the season.
def broncos_case():
    team = Team(team_id=1, name="Mine", roster=[
        wr(1, "Starter WR", 15.0, 200.0, slot=4),
        wr(2, "Bench WR", 9.0, 125.0),
        dst(3, "Broncos D/ST", 5.1, 97.0, slot=16),
    ])
    return team, dst(4, "Giants D/ST", 7.4, 81.2), [4, 16]


def test_season_gain_is_measured_for_the_same_drop():
    team, giants, slots = broncos_case()
    evaluation = evaluate_pickup(team, giants, slots, roster_limit=3,
                                 season_fn=season_projection)
    assert evaluation.drop_player.name == "Broncos D/ST"
    assert evaluation.weekly_gain == pytest.approx(2.3)
    assert evaluation.season_gain == pytest.approx(81.2 - 97.0)


def test_a_move_that_costs_the_season_is_a_labeled_stream():
    team, giants, slots = broncos_case()
    evaluation = evaluate_pickup(team, giants, slots, roster_limit=3,
                                 season_fn=season_projection)
    assert evaluation.move_type == "stream"
    assert "One-week stream" in evaluation.note
    assert "-15.8" in evaluation.note
    # Dropping the bench receiver keeps this week's gain and both defenses.
    assert evaluation.alternative_drop.name == "Bench WR"
    assert evaluation.alternative_season_gain == pytest.approx(0.0)
    assert "Bench WR" in evaluation.note
    assert evaluation.to_dict()["move_type"] == "stream"


def test_a_real_upgrade_is_not_a_stream():
    team, _, slots = broncos_case()
    better = dst(5, "Better D/ST", 7.0, 110.0)
    evaluation = evaluate_pickup(team, better, slots, roster_limit=3,
                                 season_fn=season_projection)
    assert evaluation.move_type == "upgrade"
    assert evaluation.season_gain == pytest.approx(13.0)
    assert evaluation.note == ""


def test_streams_rank_below_upgrades_and_bid_for_one_week():
    team, giants, slots = broncos_case()
    upgrade = dst(5, "Better D/ST", 6.8, 110.0)
    recs = recommend_pickups(team, [giants, upgrade], slots, roster_limit=3,
                             faab_remaining=100.0, weeks_left=15, min_gain=0.0)
    assert [r.player.name for r in recs] == ["Better D/ST", "Giants D/ST"]
    stream = recs[1]
    assert stream.suggested_bid == suggest_faab_bid(stream.weekly_gain, 100.0, weeks_left=1)
