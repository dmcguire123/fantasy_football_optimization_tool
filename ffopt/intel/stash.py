"""
Stash value: what a player is worth to your team even if he never starts.

Four pieces, each in expected points over the rest of the season:

  1. Injury cover. If one of your starters were out, how much would this
     player recover? Weighted by how often starters miss games, and by the
     starter's current injury status.
  2. Handcuff. A backup to one of your own starters (same NFL team, same
     position, running back or quarterback) is assumed to inherit most of
     that starter's role if he goes down, so his cover value is larger than
     his own projection suggests.
  3. Bye-week cover. Weeks where players on your roster are on bye and a
     lineup slot would otherwise be filled by someone weaker.
  4. Rival threat. Points that other teams would gain by adding him, a
     small credit for keeping him away from teams that would start him.

Future weeks have no projections, so a "typical week" for a player is his
season projection spread over 17 games, falling back to this week's number.
"""

from dataclasses import dataclass, field, replace

from .. import constants as C
from ..optimizer import best_possible_total, week_projection


GAMES_IN_SEASON = 17

# Chance a given starter misses any one week, before status is considered.
BASE_MISS_RATE = 0.08

# Extra weeks a starter is expected to miss because of his current status.
STATUS_EXTRA_WEEKS = {
    "OUT": 3.0,
    "INJURY_RESERVE": 6.0,
    "SUSPENSION": 3.0,
    "DOUBTFUL": 2.0,
    "QUESTIONABLE": 0.5,
}

# A handcuff is assumed to score this share of the starter he backs up.
HANDCUFF_INHERIT_SHARE = 0.65
HANDCUFF_POSITIONS = ("RB", "QB")

# Rivals who would gain at least this much count as competition, and each
# gained point is worth this much to us, capped so it stays a tiebreaker.
RIVAL_MIN_GAIN = 0.5
RIVAL_POINT_VALUE = 0.1
RIVAL_CREDIT_CAP = 3.0

# A player who is hurt himself is worth less as insurance.
CANDIDATE_OUT_FACTOR = 0.3
CANDIDATE_OUT_STATUSES = ("OUT", "INJURY_RESERVE", "SUSPENSION")

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


# A player's typical week, used when no weekly projection exists.
def typical_week(player):
    if player.season_projected_points:
        return player.season_projected_points / GAMES_IN_SEASON
    return player.projected_points


def by_week_fn(bye_by_team, week):
    def projection(player):
        if bye_by_team.get(player.pro_team) == week:
            return 0.0
        return typical_week(player)

    return projection


@dataclass
class StashContext:
    """Everything about my team and the league that does not depend on the
    candidate, worked out once and reused for every candidate."""

    roster: list
    starting_slots: list
    starters: list
    weeks_left: int
    bye_by_team: dict
    base_total: float
    without_starter: dict
    bye_weeks: list
    bye_bases: dict
    rivals: list
    base_week_total: float


# Solve the candidate-independent totals once.
def build_context(team, other_teams, starting_slots, week, final_week, bye_by_team):
    roster = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]
    starters = [p for p in roster if p.is_starting]
    weeks_left = max(1, final_week - week + 1)

    base_total = best_possible_total(roster, starting_slots, typical_week)
    without_starter = {}
    for starter in starters:
        rest = [p for p in roster if p.player_id != starter.player_id]
        without_starter[starter.player_id] = best_possible_total(
            rest, starting_slots, typical_week
        )

    # Future weeks in which someone on my roster is on bye.
    bye_weeks = sorted(
        {
            bye_by_team[p.pro_team]
            for p in roster
            if bye_by_team.get(p.pro_team) and week < bye_by_team[p.pro_team] <= final_week
        }
    )
    bye_bases = {
        w: best_possible_total(roster, starting_slots, by_week_fn(bye_by_team, w))
        for w in bye_weeks
    }

    rivals = []
    for other in other_teams:
        other_roster = [p for p in other.roster if p.lineup_slot != C.IR_SLOT]
        rivals.append(
            {
                "team": other,
                "roster": other_roster,
                "base": best_possible_total(other_roster, starting_slots, typical_week),
            }
        )

    return StashContext(
        roster=roster,
        starting_slots=starting_slots,
        starters=starters,
        weeks_left=weeks_left,
        bye_by_team=bye_by_team,
        base_total=base_total,
        without_starter=without_starter,
        bye_weeks=bye_weeks,
        bye_bases=bye_bases,
        rivals=rivals,
        base_week_total=best_possible_total(roster, starting_slots, week_projection),
    )


# Which of my starters this candidate would back up.
def handcuff_targets(candidate, starters):
    if candidate.position not in HANDCUFF_POSITIONS:
        return []
    return [
        s for s in starters
        if s.pro_team == candidate.pro_team
        and s.position == candidate.position
        and s.pro_team not in ("", "FA")
    ]


# The candidate as he would perform if this starter were out.
def as_replacement(candidate, starter, handcuffs):
    if starter not in handcuffs:
        return candidate
    inherited = max(typical_week(candidate), HANDCUFF_INHERIT_SHARE * typical_week(starter))
    return replace(candidate, projected_points=inherited, season_projected_points=0.0)


def expected_missed_weeks(starter, weeks_left):
    extra = STATUS_EXTRA_WEEKS.get((starter.injury_status or "").upper(), 0.0)
    return min(float(weeks_left), BASE_MISS_RATE * weeks_left + extra)


@dataclass
class StashProfile:
    score: float = 0.0
    injury_cover: float = 0.0
    bye_cover: float = 0.0
    rival_credit: float = 0.0
    handcuff_of: list = field(default_factory=list)
    cover_for: list = field(default_factory=list)
    bye_gaps: list = field(default_factory=list)
    rivals_gaining: int = 0
    start_now_gain: float = 0.0
    reasons: list = field(default_factory=list)

    def to_dict(self):
        return {
            "score": round(self.score, 2),
            "injury_cover": round(self.injury_cover, 2),
            "bye_cover": round(self.bye_cover, 2),
            "rival_credit": round(self.rival_credit, 2),
            "handcuff_of": self.handcuff_of,
            "cover_for": self.cover_for,
            "bye_gaps": self.bye_gaps,
            "rivals_gaining": self.rivals_gaining,
            "start_now_gain": round(self.start_now_gain, 2),
            "reasons": self.reasons,
        }


# Score one candidate against the context.
def profile(candidate, ctx):
    result = StashProfile()
    handcuffs = handcuff_targets(candidate, ctx.starters)
    result.handcuff_of = [s.name for s in handcuffs]

    # 1 and 2: cover for each starter, with handcuff inheritance.
    for starter in ctx.starters:
        stand_in = as_replacement(candidate, starter, handcuffs)
        rest = [p for p in ctx.roster if p.player_id != starter.player_id]
        with_candidate = best_possible_total(rest + [stand_in], ctx.starting_slots, typical_week)
        gain = with_candidate - ctx.without_starter[starter.player_id]
        if gain <= 0.05:
            continue

        weeks = expected_missed_weeks(starter, ctx.weeks_left)
        points = gain * weeks
        result.injury_cover += points
        result.cover_for.append(
            {"starter": starter.name, "gain_per_week": round(gain, 1), "expected_points": round(points, 1)}
        )
    result.cover_for.sort(key=lambda row: row["expected_points"], reverse=True)

    # 3: bye weeks where he fills a hole.
    for w in ctx.bye_weeks:
        fn = by_week_fn(ctx.bye_by_team, w)
        total = best_possible_total(ctx.roster + [candidate], ctx.starting_slots, fn)
        gain = total - ctx.bye_bases[w]
        if gain > 0.05:
            result.bye_cover += gain
            result.bye_gaps.append({"week": w, "gain": round(gain, 1)})

    # 4: what rivals would gain.
    gained = 0.0
    for rival in ctx.rivals:
        total = best_possible_total(rival["roster"] + [candidate], ctx.starting_slots, typical_week)
        gain = total - rival["base"]
        if gain >= RIVAL_MIN_GAIN:
            result.rivals_gaining += 1
            gained += gain
    result.rival_credit = min(RIVAL_CREDIT_CAP, RIVAL_POINT_VALUE * gained)

    # Does he also help right now? Only used to label the row.
    result.start_now_gain = (
        best_possible_total(ctx.roster + [candidate], ctx.starting_slots, week_projection)
        - ctx.base_week_total
    )

    # An injured stash is worth less as insurance.
    scale = 1.0
    if (candidate.injury_status or "").upper() in CANDIDATE_OUT_STATUSES:
        scale = CANDIDATE_OUT_FACTOR
        result.reasons.append(f"He is {candidate.injury_status.title()} himself, so his cover value is discounted.")

    result.injury_cover *= scale
    result.bye_cover *= scale
    result.score = result.injury_cover + result.bye_cover + result.rival_credit

    if handcuffs:
        result.reasons.append("Handcuff to " + ", ".join(result.handcuff_of) + ".")
    if result.cover_for:
        top = result.cover_for[0]
        result.reasons.append(
            f"Covers {top['starter']} (+{top['gain_per_week']} per week if he is out)."
        )
    if result.bye_gaps:
        weeks = ", ".join(f"wk {g['week']}" for g in result.bye_gaps)
        result.reasons.append(f"Fills a bye-week hole in {weeks}.")
    if result.rivals_gaining:
        result.reasons.append(f"{result.rivals_gaining} rival team(s) would gain from adding him.")
    return result


# Stash bids are small on purpose: this is insurance, not a starter.
STASH_MAX_SHARE = 0.08
STASH_POINT_SHARE = 0.004
STASH_MIN_SCORE = 1.0

# Shown in the list above this, even when too small to justify a bid.
STASH_LIST_MIN_SCORE = 0.1
POSITION_CAPS = {"K": 0.03, "D/ST": 0.05}


def stash_bid(score, faab_remaining, position, rivals_gaining=0):
    if faab_remaining <= 0 or score < STASH_MIN_SCORE:
        return {"percent": 0.0, "dollars": 0}

    share = STASH_POINT_SHARE * score * (1.0 + 0.05 * min(3, rivals_gaining))
    share = min(share, STASH_MAX_SHARE, POSITION_CAPS.get(position, STASH_MAX_SHARE))
    dollars = max(1, round(faab_remaining * share))
    return {"percent": round(100.0 * dollars / faab_remaining, 1), "dollars": dollars}


# Turn ESPN's bye-week map (keyed by team id) into one keyed by abbreviation.
def bye_map_by_abbrev(bye_weeks_by_id):
    return {C.pro_team_abbrev(team_id): week for team_id, week in bye_weeks_by_id.items()}
