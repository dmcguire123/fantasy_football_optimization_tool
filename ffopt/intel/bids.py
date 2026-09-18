"""FAAB bid advice as a percent of the budget you have left."""

from ..waivers import suggest_faab_bid


# Rough depth each position wants on a roster. A team below this is a
# likely bidder when a player at that position comes available.
MIN_DEPTH = {"QB": 2, "RB": 4, "WR": 4, "TE": 2, "K": 1, "D/ST": 1}

# Consensus and rival demand can nudge the bid up, never past this share.
MAX_SHARE = 0.5

# Kickers and defenses are streamed week to week, so they never get a big bid.
POSITION_MAX_SHARE = {"K": 0.03, "D/ST": 0.05}
CONSENSUS_STEP = 0.10
MAX_CONSENSUS_BOOST = 0.40
MAX_DEMAND_BOOST = 0.25


# How many other teams are thin at this position and can afford a real bid.
def rival_demand(league, my_team_id, position, my_bid):
    rivals = 0
    for team in league.teams:
        if team.team_id == my_team_id:
            continue
        depth = team.position_counts().get(position, 0)
        if depth < MIN_DEPTH.get(position, 2) and team.faab_remaining > max(1.0, my_bid * 0.5):
            rivals += 1
    return rivals


# Combine the lineup-gain bid with expert consensus and rival demand.
# Returns {percent, dollars, reasons}. Dollars are whole and at least $1
# whenever we advise bidding at all.
def bid_advice(
    weekly_gain,
    faab_remaining,
    weeks_left,
    source_count,
    position,
    league,
    my_team_id,
    season_gain=0.0,
):
    if faab_remaining <= 0:
        return {"percent": 0.0, "dollars": 0, "reasons": ["No FAAB left."]}

    reasons = []
    base = suggest_faab_bid(weekly_gain, faab_remaining, weeks_left=weeks_left)

    # A stash with no weekly gain but season value still earns a small bid.
    if base == 0 and season_gain > 0:
        base = max(1.0, round(faab_remaining * 0.01))
        reasons.append("Bench stash: value shows up over the season.")
    elif base > 0:
        reasons.append(f"Adds {weekly_gain:.1f} projected points to your lineup.")

    if base == 0 and source_count < 2:
        return {"percent": 0.0, "dollars": 0, "reasons": ["No lineup gain and little expert interest."]}
    if base == 0:
        base = 1.0
        reasons.append("Experts like him, but he does not help your lineup now.")

    consensus_boost = min(MAX_CONSENSUS_BOOST, CONSENSUS_STEP * max(0, source_count - 1))
    if consensus_boost:
        reasons.append(f"Named by {source_count} sources.")

    rivals = rival_demand(league, my_team_id, position, base)
    demand_boost = min(MAX_DEMAND_BOOST, 0.05 * rivals)
    if rivals:
        reasons.append(f"{rivals} other team(s) are thin at {position} and have budget.")

    dollars = base * (1.0 + consensus_boost + demand_boost)
    cap = POSITION_MAX_SHARE.get(position, MAX_SHARE)
    dollars = min(dollars, faab_remaining * cap)
    dollars = max(1, round(dollars))

    percent = round(100.0 * dollars / faab_remaining, 1)
    return {"percent": percent, "dollars": dollars, "reasons": reasons}
