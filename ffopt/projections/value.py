"""
Who is undervalued this week, and by how much we should believe it.

Two kinds of mispricing are worth acting on:

  1. Our model against the experts. On its own the model is less accurate,
     but where it disagrees with the experts' blend, part of that
     disagreement shows up in the result. The backtest measured how much at
     each position (the "edge slope", about 0.2), so a model that sits 5
     points above the experts means about 1 point of expected edge. Only
     the expected edge is reported, never the raw gap.

  2. The experts against ESPN. Everyone else in an ESPN league sees ESPN's
     projections, so a player the other sources rate well above ESPN is one
     the rest of the league is likely to overlook. ESPN also leaves players
     at zero after a role change, which this catches.

The report covers available players (pickups) and your own roster (who to
worry about).
"""

from .backtest import load_results
from .consensus import MIN_RELEVANT_POINTS, blend
from .service import projected_only


# Edges smaller than these are noise, not worth a roster move.
MIN_EXPECTED_EDGE = 0.4
MIN_ESPN_GAP = 2.0

SKILL_POSITIONS = ("QB", "RB", "WR", "TE")


# The experts' blend for a player, leaving our model out.
def experts_blend(player):
    experts = {s: v for s, v in player.source_points.items() if s != "model"}
    if not experts:
        return None
    mean, _ = blend(projected_only(experts))
    return mean


# The other experts' blend, leaving out ESPN and our model.
def non_espn_blend(player):
    others = {
        s: v for s, v in player.source_points.items() if s not in ("espn", "model")
    }
    if not others:
        return None
    mean, _ = blend(projected_only(others))
    return mean


def player_line(player, **extra):
    return {
        "player_id": player.player_id,
        "name": player.name,
        "position": player.position,
        "pro_team": player.pro_team,
        "injury_status": player.injury_status,
        "percent_owned": round(player.percent_owned, 1),
        "espn": round(player.espn_projected_points, 2),
        "consensus": round(player.consensus_points, 2),
        "sources": {k: round(v, 2) for k, v in player.source_points.items()},
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in extra.items()},
    }


# The model's expected edge over the experts for one player, or None. The
# backtest only graded players the experts expected to play a real role, so
# a player they project near zero (a backup, a benched starter) is left
# out: the model knows his past games but not that he lost the job.
def model_edge(player, slopes):
    if player.position not in SKILL_POSITIONS or "model" not in player.source_points:
        return None
    experts = experts_blend(player)
    if experts is None or experts < MIN_RELEVANT_POINTS or not player.is_playable:
        return None
    gap = player.source_points["model"] - experts
    return gap, slopes.get(player.position, 0.0) * gap, experts


# Build the report from players that already carry their per-source
# projections (see service.apply).
def value_report(my_roster, available, results=None, limit=15):
    results = results if results is not None else (load_results() or {})
    slopes = results.get("slopes") or {}

    model_pickups = []
    for player in available:
        edge = model_edge(player, slopes)
        if edge and edge[1] >= MIN_EXPECTED_EDGE:
            model_pickups.append(
                player_line(player, experts=edge[2], model=player.source_points["model"],
                            expected_edge=edge[1])
            )
    model_pickups.sort(key=lambda row: -row["expected_edge"])

    espn_behind = []
    for player in available:
        others = non_espn_blend(player)
        if others is None or not player.is_playable:
            continue
        gap = others - player.espn_projected_points
        if gap >= MIN_ESPN_GAP:
            espn_behind.append(player_line(player, others=others, gap_vs_espn=gap))
    espn_behind.sort(key=lambda row: -row["gap_vs_espn"])

    roster_warnings = []
    roster_boosts = []
    for player in my_roster:
        edge = model_edge(player, slopes)
        if not edge:
            continue
        line = player_line(player, experts=edge[2], model=player.source_points["model"],
                           expected_edge=edge[1])
        if edge[1] <= -MIN_EXPECTED_EDGE:
            roster_warnings.append(line)
        elif edge[1] >= MIN_EXPECTED_EDGE:
            roster_boosts.append(line)
    roster_warnings.sort(key=lambda row: row["expected_edge"])
    roster_boosts.sort(key=lambda row: -row["expected_edge"])

    return {
        "backtest": {
            "run_at": results.get("run_at"),
            "seasons": results.get("seasons"),
            "slopes": slopes,
            "weights": results.get("weights") or {},
        },
        "model_pickups": model_pickups[:limit],
        "espn_behind": espn_behind[:limit],
        "roster_warnings": roster_warnings[:limit],
        "roster_boosts": roster_boosts[:limit],
    }
