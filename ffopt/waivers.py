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
from .optimizer import (
    best_lineup_win_probability,
    best_possible_total,
    season_projection,
    week_projection,
)


# Players who should never be offered up as the drop side of a claim.
def is_droppable(player):
    if player.lineup_slot == C.IR_SLOT:
        return False
    return True


# A move that loses more than this over the rest of the season is a stream:
# it helps this week and costs you later.
STREAM_SEASON_COST = 1.0

# An alternative drop may give up this much of this week's gain.
ALTERNATIVE_WEEKLY_SLACK = 0.5


class PickupEvaluation:
    """What one available player would be worth to one team."""

    def __init__(
        self, player, drop_player, weekly_gain, season_gain, needs_drop, win_gain=None,
        alternative_drop=None, alternative_season_gain=None,
    ):
        self.player = player
        self.drop_player = drop_player
        self.weekly_gain = weekly_gain
        # Rest-of-season gain of the same move: this player in, drop_player out.
        self.season_gain = season_gain
        self.needs_drop = needs_drop
        self.win_gain = win_gain
        # A different drop that keeps this week's gain at less season cost.
        self.alternative_drop = alternative_drop
        self.alternative_season_gain = alternative_season_gain
        self.suggested_bid = 0.0
        self.weeks_left = 1

    # "stream": helps this week, costs points over the rest of the season.
    # "stash": nothing this week, worth something later. Otherwise "upgrade".
    @property
    def move_type(self):
        if self.weekly_gain > 0 and self.season_gain < -STREAM_SEASON_COST:
            return "stream"
        if self.weekly_gain <= 0 and self.season_gain > 0:
            return "stash"
        return "upgrade"

    # What the move is worth per week once its rest-of-season cost is spread
    # over the weeks left. Used to rank streams below true upgrades.
    @property
    def net_gain(self):
        return self.weekly_gain + min(0.0, self.season_gain) / max(1, self.weeks_left)

    # One plain sentence on what the move really costs, or "".
    @property
    def note(self):
        if self.move_type != "stream":
            return ""
        drop = self.drop_player.name if self.drop_player else "nobody"
        text = (
            f"One-week stream: {self.weekly_gain:+.1f} this week, "
            f"{self.season_gain:+.1f} over the rest of the season by dropping {drop}."
        )
        if self.alternative_drop is not None:
            alternative = self.alternative_drop
            text += (
                f" Or drop {alternative.name} ({alternative.lineup_slot_name}, "
                f"{season_projection(alternative):.0f} ROS points) to keep both: your "
                f"best lineup changes by {self.alternative_season_gain:+.1f} rest of "
                f"season, but you lose his depth for byes and injuries."
            )
        return text

    def to_dict(self):
        result = {
            "player": self.player.to_dict(),
            "drop_player": self.drop_player.to_dict() if self.drop_player else None,
            "needs_drop": self.needs_drop,
            "weekly_gain": round(self.weekly_gain, 2),
            "season_gain": round(self.season_gain, 2),
            "net_gain": round(self.net_gain, 2),
            "move_type": self.move_type,
            "note": self.note,
            "alternative_drop": (
                self.alternative_drop.to_dict() if self.alternative_drop else None
            ),
            "alternative_season_gain": (
                round(self.alternative_season_gain, 2)
                if self.alternative_season_gain is not None else None
            ),
            "suggested_bid": round(self.suggested_bid, 2),
        }
        if self.win_gain is not None:
            result["win_probability_gain"] = round(self.win_gain, 4)
        return result


# Score one available player against one roster. Tries every legal drop and
# keeps the one that leaves the strongest lineup this week. With season_fn,
# also measures that same move over the rest of the season, and looks for a
# different drop that keeps this week's gain without the season cost.
def evaluate_pickup(
    team,
    candidate,
    starting_slots,
    roster_limit=0,
    projection_fn=week_projection,
    opponent=None,
    season_fn=None,
):
    roster = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]
    base_total = best_possible_total(roster, starting_slots, projection_fn)
    base_season = (
        best_possible_total(roster, starting_slots, season_fn) if season_fn else 0.0
    )

    def season_gain_of(pool):
        if not season_fn:
            return 0.0
        return best_possible_total(pool, starting_slots, season_fn) - base_season

    # If there is an open roster spot, adding without dropping is allowed.
    needs_drop = bool(roster_limit) and len(roster) >= roster_limit

    # (drop or None, weekly gain, season gain) for every legal move.
    moves = []
    if not needs_drop:
        pool = roster + [candidate]
        total = best_possible_total(pool, starting_slots, projection_fn)
        moves.append((None, total - base_total, season_gain_of(pool)))
    for drop in roster:
        if not is_droppable(drop):
            continue
        pool = [p for p in roster if p.player_id != drop.player_id] + [candidate]
        total = best_possible_total(pool, starting_slots, projection_fn)
        moves.append((drop, total - base_total, season_gain_of(pool)))

    best = None
    for move in moves:
        drop, gain, _ = move
        if best is None:
            best = move
            continue
        # On a tie, give up whoever is worth the least over the season, so a
        # move that changes nothing this week never suggests cutting a starter.
        is_tie = abs(gain - best[1]) < 1e-9
        cheaper = (
            drop is not None and best[0] is not None
            and season_projection(drop) < season_projection(best[0])
        )
        if gain > best[1] + 1e-9 or (is_tie and cheaper):
            best = move

    best_drop, best_gain, season_gain = best if best else (None, 0.0, 0.0)

    # A stream that costs season points: is there a drop that keeps nearly
    # all of this week's gain and costs less over the season?
    alternative = None
    if season_fn and best and season_gain < -STREAM_SEASON_COST:
        for move in moves:
            drop, gain, season = move
            if drop is best_drop or gain < best_gain - ALTERNATIVE_WEEKLY_SLACK:
                continue
            if season <= season_gain + STREAM_SEASON_COST:
                continue
            if (
                alternative is None
                or season > alternative[2] + 1e-9
                or (abs(season - alternative[2]) < 1e-9 and drop is not None
                    and alternative[0] is not None
                    and season_projection(drop) < season_projection(alternative[0]))
            ):
                alternative = move

    # With an opponent known, also report how much the move changes the
    # chance of winning this week's matchup.
    win_gain = None
    if opponent is not None:
        after = [p for p in roster if best_drop is None or p.player_id != best_drop.player_id]
        win_gain = best_lineup_win_probability(
            after + [candidate], starting_slots, opponent, projection_fn
        ) - best_lineup_win_probability(roster, starting_slots, opponent, projection_fn)

    return PickupEvaluation(
        player=candidate,
        drop_player=best_drop,
        weekly_gain=best_gain or 0.0,
        season_gain=season_gain,
        needs_drop=needs_drop,
        win_gain=win_gain,
        alternative_drop=alternative[0] if alternative else None,
        alternative_season_gain=alternative[2] if alternative else None,
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
    opponent=None,
    objective="points",
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
        # Measured this week and, for the same move, over the rest of the
        # season, so a one-week plug that costs a better long-term player
        # says so.
        evaluation = evaluate_pickup(
            team,
            candidate,
            starting_slots,
            roster_limit=roster_limit,
            projection_fn=week_fn,
            opponent=opponent,
            season_fn=season_fn,
        )
        evaluation.weeks_left = weeks_left

        # A stream only pays off this week, so bid for one week.
        evaluation.suggested_bid = suggest_faab_bid(
            evaluation.weekly_gain,
            faab_remaining,
            weeks_left=1 if evaluation.move_type == "stream" else weeks_left,
        )
        evaluations.append(evaluation)

    evaluations = [e for e in evaluations if e.weekly_gain >= min_gain or e.season_gain > 0]
    if objective == "win" and opponent is not None:
        evaluations.sort(
            key=lambda e: (e.win_gain or 0.0, e.net_gain, e.season_gain), reverse=True
        )
    else:
        evaluations.sort(key=lambda e: (e.net_gain, e.season_gain), reverse=True)
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
