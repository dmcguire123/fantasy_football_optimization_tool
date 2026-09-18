"""
Waiver wire evaluation and claim submission.

A pickup is only worth what it adds to the lineup you can actually start.
So every free agent is scored by re-solving the optimal lineup with that
player added and one of your players dropped, and taking the difference.
That automatically discounts a good player at a position where you are
already deep, and rewards a mediocre one at a position where you are thin.
"""

from . import constants as C
from .espn_client import build_add_drop_payload
from .optimizer import best_possible_total, season_projection, week_projection


# Players who should never be offered up as the drop side of a claim.
def is_droppable(player):
    if player.lineup_slot == C.IR_SLOT:
        return False
    return True


class PickupEvaluation:
    """What one available player would be worth to one team."""

    def __init__(self, player, drop_player, weekly_gain, season_gain, needs_drop):
        self.player = player
        self.drop_player = drop_player
        self.weekly_gain = weekly_gain
        self.season_gain = season_gain
        self.needs_drop = needs_drop
        self.suggested_bid = 0.0

    def to_dict(self):
        return {
            "player": self.player.to_dict(),
            "drop_player": self.drop_player.to_dict() if self.drop_player else None,
            "needs_drop": self.needs_drop,
            "weekly_gain": round(self.weekly_gain, 2),
            "season_gain": round(self.season_gain, 2),
            "suggested_bid": round(self.suggested_bid, 2),
        }


# Score one available player against one roster. Tries every legal drop and
# keeps the one that leaves the strongest lineup.
def evaluate_pickup(
    team,
    candidate,
    starting_slots,
    roster_limit=0,
    projection_fn=week_projection,
):
    roster = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]
    base_total = best_possible_total(roster, starting_slots, projection_fn)

    # If there is an open roster spot, adding without dropping is allowed.
    needs_drop = bool(roster_limit) and len(roster) >= roster_limit

    best_gain = None
    best_drop = None

    if not needs_drop:
        total = best_possible_total(roster + [candidate], starting_slots, projection_fn)
        best_gain = total - base_total
        best_drop = None

    for drop in roster:
        if not is_droppable(drop):
            continue
        pool = [p for p in roster if p.player_id != drop.player_id] + [candidate]
        total = best_possible_total(pool, starting_slots, projection_fn)
        gain = total - base_total
        # On a tie, give up whoever is worth the least over the season, so a
        # move that changes nothing this week never suggests cutting a starter.
        is_tie = best_gain is not None and abs(gain - best_gain) < 1e-9
        cheaper = best_drop is not None and season_projection(drop) < season_projection(best_drop)
        if best_gain is None or gain > best_gain + 1e-9 or (is_tie and cheaper):
            best_gain = gain
            best_drop = drop

    return PickupEvaluation(
        player=candidate,
        drop_player=best_drop,
        weekly_gain=best_gain or 0.0,
        season_gain=0.0,
        needs_drop=needs_drop,
    )


# Convert a weekly point gain into a FAAB bid, as a share of what is left in
# the budget. The multiplier is deliberately conservative: a pickup worth
# three points a week is worth roughly a tenth of a remaining budget, and no
# single claim is ever advised above half of it.
def suggest_faab_bid(weekly_gain, faab_remaining, weeks_left=1, max_share=0.5):
    if faab_remaining <= 0 or weekly_gain <= 0:
        return 0.0

    # More weeks left means the pickup pays off more times, but the budget
    # also has to cover more future claims, so the horizon is dampened.
    horizon_factor = min(2.0, 1.0 + (max(0, weeks_left - 1) * 0.08))
    share = min(max_share, weekly_gain * 0.035 * horizon_factor)

    bid = faab_remaining * share
    if bid < 1.0:
        return 1.0
    return round(bid)


# Rank the available player pool by what it would add to this team.
def recommend_pickups(
    team,
    free_agents,
    starting_slots,
    roster_limit=0,
    faab_remaining=0.0,
    weeks_left=1,
    limit=15,
    min_gain=0.1,
    evaluate_top=80,
    positions=None,
    week_fn=week_projection,
    season_fn=season_projection,
):
    candidates = list(free_agents)

    if positions:
        wanted = {p.upper() for p in positions}
        candidates = [p for p in candidates if p.position.upper() in wanted]

    # Evaluating every player in the pool is wasteful, and the pool is
    # already sorted by ownership. Score the most relevant slice properly.
    candidates.sort(
        key=lambda p: (p.effective_projection, p.percent_owned), reverse=True
    )
    candidates = candidates[: max(evaluate_top, limit)]

    evaluations = []
    for candidate in candidates:
        evaluation = evaluate_pickup(
            team,
            candidate,
            starting_slots,
            roster_limit=roster_limit,
            projection_fn=week_fn,
        )

        # Also measure the move over the rest of the season, which is what
        # matters for a bench stash rather than a one-week plug.
        season_eval = evaluate_pickup(
            team,
            candidate,
            starting_slots,
            roster_limit=roster_limit,
            projection_fn=season_fn,
        )
        evaluation.season_gain = season_eval.weekly_gain

        evaluation.suggested_bid = suggest_faab_bid(
            evaluation.weekly_gain, faab_remaining, weeks_left=weeks_left
        )
        evaluations.append(evaluation)

    evaluations = [e for e in evaluations if e.weekly_gain >= min_gain or e.season_gain > 0]
    evaluations.sort(key=lambda e: (e.weekly_gain, e.season_gain), reverse=True)
    return evaluations[:limit]


# Roster players the marginal analysis says are the cheapest to lose.
def drop_candidates(team, starting_slots, limit=5):
    roster = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]
    base_week = best_possible_total(roster, starting_slots, week_projection)
    base_season = best_possible_total(roster, starting_slots, season_projection)

    ranked = []
    for player in roster:
        if not is_droppable(player):
            continue
        pool = [p for p in roster if p.player_id != player.player_id]
        week_cost = base_week - best_possible_total(pool, starting_slots, week_projection)
        season_cost = base_season - best_possible_total(
            pool, starting_slots, season_projection
        )
        ranked.append(
            {
                "player": player.to_dict(),
                "weekly_cost": round(week_cost, 2),
                "season_cost": round(season_cost, 2),
            }
        )

    ranked.sort(key=lambda row: (row["season_cost"], row["weekly_cost"]))
    return ranked[:limit]


# Build the ESPN transaction for a claim. Waiver claims are queued and
# processed later; free agent adds apply immediately.
def build_claim(
    team_id,
    member_id,
    week,
    add_player_id,
    drop_player_id=None,
    bid_amount=None,
    is_waiver=True,
):
    return build_add_drop_payload(
        team_id=team_id,
        member_id=member_id,
        week=week,
        add_player_id=add_player_id,
        drop_player_id=drop_player_id,
        bid_amount=bid_amount,
        transaction_type="WAIVER" if is_waiver else "FREEAGENT",
    )
