"""Waiver evaluation, bid sizing, and claim payloads."""

import json

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
