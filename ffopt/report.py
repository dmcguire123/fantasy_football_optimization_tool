"""
The morning report: everything worth knowing today, in one JSON document.

This gathers what the rest of the app already works out (the best lineup,
this week's matchup, waiver targets, undervalued players, injuries on your
roster) so that one command can feed an email or any other summary. It only
reads; nothing here changes your roster.
"""

import datetime

from . import constants as C
from . import scouting, waivers
from .optimizer import optimize_team
from .projections.value import value_report


def _player(player):
    return {
        "name": player.name,
        "position": player.position,
        "pro_team": player.pro_team,
        "slot": player.lineup_slot_name,
        "projection": round(player.effective_projection, 1),
        "espn": round(player.espn_projected_points, 1),
        "sources": {k: round(v, 1) for k, v in player.source_points.items()},
        "injury_status": C.INJURY_STATUS_NAMES.get(player.injury_status, player.injury_status),
        "on_bye": player.on_bye,
    }


# Starters who are hurt, on bye, or doubtful, and bench players who are not.
def roster_alerts(team):
    alerts = []
    for player in team.roster:
        if player.lineup_slot == C.IR_SLOT:
            continue
        flagged = player.on_bye or player.injury_status not in ("ACTIVE", "NORMAL")
        if flagged:
            alerts.append({**_player(player), "starting": player.is_starting})
    return alerts


# Projection moves worth a line in the email: your players whose weekly
# projection jumped or dropped, and rest-of-season risers and fallers among
# your players and the available ones.
def trend_summary(service, team, pool, week):
    try:
        from .projections.trends import ros_movers, weekly_movers

        data = service.trends(week)
    except Exception:
        return None
    if data is None:
        return None
    mine = {str(p.player_id) for p in team.roster}
    available = {str(p.player_id) for p in pool}
    my_weekly = weekly_movers(data, limit=5, only=mine)
    my_ros = ros_movers(data, limit=5, only=mine)
    free_ros = ros_movers(data, limit=5, only=available)
    return {
        "my_weekly_moves": my_weekly["risers"] + my_weekly["fallers"],
        "my_ros_moves": my_ros["risers"] + my_ros["fallers"],
        "available_ros_risers": free_ros["risers"],
        "ros_days_of_history": my_ros["days_of_history"],
    }


def build_report(service, week=None, today=None):
    today = today or datetime.date.today()
    league = service.load_league(week)
    team = service.my_team(week)
    slots = league.settings.starting_slots()
    opponent = scouting.opponent_distribution(league, team, league.week)

    lineup = optimize_team(team, slots, objective="win", opponent=opponent)
    matchup = scouting.opponent_report(league, team, slots, week=league.week)

    pool = service.free_agents(week=league.week, limit=150)
    pickups = waivers.recommend_pickups(
        team,
        pool,
        slots,
        roster_limit=league.settings.active_roster_size,
        faab_remaining=team.faab_remaining,
        weeks_left=max(1, league.settings.final_week - league.week + 1),
        limit=5,
        opponent=opponent,
    )

    value = value_report(team.roster, pool, limit=5)
    trends = trend_summary(service, team, pool, league.week)

    return {
        "date": today.isoformat(),
        "weekday": today.strftime("%A"),
        "league": league.settings.name,
        "week": league.week,
        "team": team.name,
        "record": team.record,
        "projection_source": service.settings.projection_source,
        "lineup": {
            "moves": lineup.moves,
            "points_gained": round(lineup.points_gained, 1),
            "optimal_total": round(lineup.total, 1),
            "win_probability": lineup.win_probability,
            "win_probability_as_set": lineup.current_win_probability,
            "starters": [_player(p) for _, p in lineup.assignments if p],
        },
        "matchup": {
            "has_opponent": matchup.get("has_opponent", False),
            "opponent": (matchup.get("opponent") or {}).get("name"),
            "opponent_record": (matchup.get("opponent") or {}).get("record"),
            "my_projected": matchup.get("my_projected_best"),
            "their_projected": matchup.get("their_projected_current"),
            "margin": matchup.get("projected_margin"),
            "their_risks": [
                {"name": p["name"], "position": p["position"],
                 "status": "BYE" if p["on_bye"] else p["injury_status"]}
                for p in matchup.get("their_risks") or []
            ],
        },
        "roster_alerts": roster_alerts(team),
        "waivers": {
            "uses_faab": league.settings.uses_faab,
            "faab_remaining": team.faab_remaining,
            "targets": [
                {
                    **_player(rec.player),
                    "weekly_gain": round(rec.weekly_gain, 1),
                    "season_gain": round(rec.season_gain, 1),
                    "drop": rec.drop_player.name if rec.drop_player else None,
                    "bid": round(rec.suggested_bid) if league.settings.uses_faab else None,
                    "player_id": rec.player.player_id,
                }
                for rec in pickups
            ],
        },
        "trends": trends,
        "value": {
            "model_pickups": value["model_pickups"],
            "espn_behind": value["espn_behind"],
            "roster_warnings": value["roster_warnings"],
            "roster_boosts": value["roster_boosts"],
        },
    }
