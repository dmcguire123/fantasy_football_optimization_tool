"""
League-wide analysis: who is strong, who you are playing, and where the
roster imbalances are.

Everything here is built on the same optimal-lineup solver used for your own
team, applied to every roster in the league. That makes the comparisons fair:
a team is measured by the best lineup it could start, not the one it has
carelessly left in place.
"""

import math

from . import constants as C
from .optimizer import best_possible_total, solve_lineup, week_projection


# Typical week-to-week spread of a fantasy team's score. Used only to turn a
# projected margin into a win probability, so it does not need to be exact.
WEEKLY_SCORE_STDDEV = 27.0


# Probability the first score beats the second, assuming both are normally
# distributed around their projections with the spread above.
def win_probability(projected_for, projected_against):
    margin = projected_for - projected_against
    combined_stddev = WEEKLY_SCORE_STDDEV * math.sqrt(2)
    z = margin / combined_stddev
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


# Points a team is leaving on its bench this week: the gap between the
# lineup it has set and the best one available to it.
def lineup_efficiency(team, starting_slots):
    current = team.projected_starting_points
    best = best_possible_total(team.roster, starting_slots, week_projection)
    return {
        "current_projected": round(current, 2),
        "best_projected": round(best, 2),
        "points_left_on_bench": round(max(0.0, best - current), 2),
        "efficiency": round(current / best, 3) if best > 0 else 1.0,
    }


# Rank every team by a blend of what it has done and what it can do now.
# Season scoring shows true strength; the current optimal lineup shows how
# dangerous the roster is this week.
def power_rankings(league, starting_slots):
    rows = []
    for team in league.teams:
        played = max(1, team.wins + team.losses + team.ties)
        best_now = best_possible_total(team.roster, starting_slots, week_projection)
        rows.append(
            {
                "team": team.to_dict(include_roster=False),
                "points_per_game": round(team.points_for / played, 2),
                "roster_strength_this_week": round(best_now, 2),
                "efficiency": lineup_efficiency(team, starting_slots),
            }
        )

    # Normalize both signals to 0-1 so neither dominates the blend.
    def spread(values):
        low, high = min(values), max(values)
        if high - low < 1e-9:
            return lambda v: 0.5
        return lambda v: (v - low) / (high - low)

    scale_ppg = spread([r["points_per_game"] for r in rows])
    scale_now = spread([r["roster_strength_this_week"] for r in rows])

    for row in rows:
        season_signal = scale_ppg(row["points_per_game"])
        current_signal = scale_now(row["roster_strength_this_week"])
        row["power_score"] = round(100 * (0.55 * season_signal + 0.45 * current_signal), 1)

    rows.sort(key=lambda r: r["power_score"], reverse=True)
    for index, row in enumerate(rows, start=1):
        row["power_rank"] = index

    return rows


# How two rosters compare at each starting position group this week.
def positional_matchup(team, opponent, starting_slots):
    mine, _ = solve_lineup(
        [p for p in team.roster if p.lineup_slot != C.IR_SLOT],
        starting_slots,
        week_projection,
    )
    theirs, _ = solve_lineup(
        [p for p in opponent.roster if p.lineup_slot != C.IR_SLOT],
        starting_slots,
        week_projection,
    )

    def totals_by_slot(assignments):
        totals = {}
        for slot_id, player in assignments:
            name = C.slot_name(slot_id)
            totals[name] = totals.get(name, 0.0) + (
                player.effective_projection if player else 0.0
            )
        return totals

    my_totals = totals_by_slot(mine)
    their_totals = totals_by_slot(theirs)

    rows = []
    for slot_name in sorted(set(my_totals) | set(their_totals)):
        mine_points = my_totals.get(slot_name, 0.0)
        theirs_points = their_totals.get(slot_name, 0.0)
        rows.append(
            {
                "slot_name": slot_name,
                "my_points": round(mine_points, 2),
                "their_points": round(theirs_points, 2),
                "edge": round(mine_points - theirs_points, 2),
            }
        )

    rows.sort(key=lambda r: r["edge"])
    return rows


# A full scouting report on this week's opponent.
def opponent_report(league, team, starting_slots, week=None):
    week = week or league.week
    opponent = league.opponent_for(team.team_id, week)

    if not opponent:
        return {
            "week": week,
            "has_opponent": False,
            "message": "No opponent scheduled for this week.",
        }

    my_best = best_possible_total(team.roster, starting_slots, week_projection)
    their_best = best_possible_total(opponent.roster, starting_slots, week_projection)
    their_current = opponent.projected_starting_points

    return {
        "week": week,
        "has_opponent": True,
        "opponent": opponent.to_dict(include_roster=False),
        "my_projected_best": round(my_best, 2),
        "my_projected_current": round(team.projected_starting_points, 2),
        "their_projected_best": round(their_best, 2),
        "their_projected_current": round(their_current, 2),
        "projected_margin": round(my_best - their_current, 2),
        "win_probability_if_optimal": round(
            win_probability(my_best, their_current), 3
        ),
        "win_probability_as_set": round(
            win_probability(team.projected_starting_points, their_current), 3
        ),
        "their_lineup_efficiency": lineup_efficiency(opponent, starting_slots),
        "positional_edges": positional_matchup(team, opponent, starting_slots),
        "their_starters": [p.to_dict() for p in opponent.starters],
        "their_risks": [
            p.to_dict()
            for p in opponent.starters
            if not p.is_playable or p.injury_status != "ACTIVE"
        ],
    }


# Where each team is deep and where it is thin, measured against what the
# league's starting requirements demand at that position.
def positional_surplus(league, starting_slots):
    # How many starters of each position the format needs, counting a FLEX
    # as a partial claim on each position it accepts.
    demand = {}
    for slot_id in starting_slots:
        slot = C.slot_name(slot_id)
        if slot in ("QB", "RB", "WR", "TE", "K", "D/ST"):
            demand[slot] = demand.get(slot, 0) + 1
        elif slot in ("FLEX", "RB/WR", "WR/TE", "OP"):
            for position in ("RB", "WR", "TE"):
                demand[position] = demand.get(position, 0) + (1 / 3)

    rows = []
    for team in league.teams:
        counts = team.position_counts()
        positions = {}
        for position, needed in demand.items():
            have = counts.get(position, 0)
            positions[position] = {
                "rostered": have,
                "starters_needed": round(needed, 2),
                "surplus": round(have - needed, 2),
            }
        rows.append(
            {
                "team": team.to_dict(include_roster=False),
                "positions": positions,
            }
        )
    return rows


# Players on other rosters who would improve your starting lineup, paired
# with what you could sensibly send back. A player is flagged as spare when
# their team is carrying more of that position than the format demands,
# which is what makes a trade plausible rather than merely desirable.
def trade_targets(
    league,
    team,
    starting_slots,
    limit_per_team=3,
    min_gain=0.5,
):
    surplus_rows = positional_surplus(league, starting_slots)
    surplus_by_team = {
        row["team"]["team_id"]: row["positions"] for row in surplus_rows
    }
    my_positions = surplus_by_team.get(team.team_id, {})

    my_roster = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]
    my_base = best_possible_total(my_roster, starting_slots, week_projection)

    entries = []
    for other in league.teams:
        if other.team_id == team.team_id:
            continue

        their_positions = surplus_by_team.get(other.team_id, {})
        their_roster = [p for p in other.roster if p.lineup_slot != C.IR_SLOT]
        their_base = best_possible_total(their_roster, starting_slots, week_projection)

        # What of theirs would help me, and can they afford to lose it.
        wants = []
        for player in their_roster:
            gain = (
                best_possible_total(
                    my_roster + [player], starting_slots, week_projection
                )
                - my_base
            )
            if gain < min_gain:
                continue

            spare = their_positions.get(player.position, {}).get("surplus", 0) >= 1
            wants.append(
                {
                    "player": player.to_dict(),
                    "weekly_gain_for_me": round(gain, 2),
                    "is_their_starter": player.is_starting,
                    "they_can_spare": bool(spare),
                }
            )

        if not wants:
            continue

        # What of mine would help them, drawn from positions where I am deep.
        offers = []
        for player in my_roster:
            if my_positions.get(player.position, {}).get("surplus", 0) < 1:
                continue
            gain = (
                best_possible_total(
                    their_roster + [player], starting_slots, week_projection
                )
                - their_base
            )
            if gain < min_gain:
                continue
            offers.append(
                {
                    "player": player.to_dict(),
                    "weekly_gain_for_them": round(gain, 2),
                    "is_my_starter": player.is_starting,
                }
            )

        # Spare players first, then by how much they help.
        wants.sort(
            key=lambda row: (row["they_can_spare"], row["weekly_gain_for_me"]),
            reverse=True,
        )
        offers.sort(key=lambda row: row["weekly_gain_for_them"], reverse=True)

        entries.append(
            {
                "team": other.to_dict(include_roster=False),
                "their_surplus_positions": [
                    position
                    for position, data in their_positions.items()
                    if data["surplus"] >= 1
                ],
                "my_surplus_positions": [
                    position
                    for position, data in my_positions.items()
                    if data["surplus"] >= 1
                ],
                "players": wants[:limit_per_team],
                "could_offer": offers[:limit_per_team],
            }
        )

    entries.sort(
        key=lambda entry: max(p["weekly_gain_for_me"] for p in entry["players"]),
        reverse=True,
    )
    return entries
