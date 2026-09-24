"""
The season dashboard: any week's matchup, and any player's week-by-week
outlook.

For the current week, a player's projection is the one the rest of the app
uses: the blend of every source, nudged toward our model where the backtest
earned it, zero if he is on bye or ruled out.

For a later week there is no single number from anyone, so it is built
here: the average of the sources that project that week (ESPN and Sleeper),
zero on the player's bye week. Our model's number for that week is shown
next to it but not blended; its future weeks haven't been backtested.
Rosters are today's rosters. Nobody knows the week 8 rosters yet, so a
future matchup answers "if both teams stood pat".
"""

from . import constants as C
from . import winprob
from .optimizer import solve_lineup
from .projections.consensus import blend
from .projections.service import projected_only


EXPERT_SOURCES = ("espn", "sleeper", "fantasypros")


class WeekProjector:
    """Projects any player for any week of the rest of the season."""

    def __init__(self, projection_set, current_week, bye_weeks, pro_schedule):
        self.projection_set = projection_set
        self.current_week = current_week
        # {pro team abbrev: bye week}
        self.bye_weeks = bye_weeks
        # {pro team abbrev: {week: ("vs" or "@", opponent abbrev)}}
        self.pro_schedule = pro_schedule

    def on_bye(self, player, week):
        return self.bye_weeks.get(player.pro_team) == week

    def nfl_game(self, player, week):
        game = self.pro_schedule.get(player.pro_team, {}).get(week)
        if not game:
            return "BYE" if self.on_bye(player, week) else ""
        return f"{game[0]} {game[1]}"

    # Every source's number for the player that week.
    def sources(self, player, week):
        if week == self.current_week:
            return dict(player.source_points)
        if self.projection_set is None:
            return {}
        return dict(self.projection_set.week_for(player, week))

    # The projection the dashboard uses for the player that week.
    def points(self, player, week):
        if week == self.current_week:
            return player.effective_projection
        if self.on_bye(player, week):
            return 0.0
        experts = {s: v for s, v in self.sources(player, week).items() if s in EXPERT_SOURCES}
        if not experts:
            return 0.0
        mean, _ = blend(projected_only(experts))
        return mean

    def function(self, week):
        return lambda player: self.points(player, week)


def _player_line(player, projector, week, slot=None):
    return {
        "player_id": player.player_id,
        "name": player.name,
        "position": player.position,
        "pro_team": player.pro_team,
        "slot": C.slot_name(slot) if slot is not None else player.lineup_slot_name,
        "projection": round(projector.points(player, week), 2),
        "sources": {k: round(v, 1) for k, v in projector.sources(player, week).items()},
        "game": projector.nfl_game(player, week),
        "injury_status": C.INJURY_STATUS_NAMES.get(player.injury_status, player.injury_status),
        "on_bye": projector.on_bye(player, week),
    }


# A team's best lineup for a week, by the week's projections.
def best_lineup(team, starting_slots, projector, week):
    projection = projector.function(week)
    startable = [p for p in team.roster if p.lineup_slot != C.IR_SLOT]
    assignments, bench = solve_lineup(startable, starting_slots, projection)
    starters = [p for _, p in assignments if p]
    mean, stddev = winprob.lineup_distribution(starters, projection)
    return {
        "team_id": team.team_id,
        "name": team.name,
        "record": team.record,
        "total": round(mean, 1),
        "stddev": stddev,
        "starters": [
            _player_line(p, projector, week, slot) if p else
            {"slot": C.slot_name(slot), "name": None, "projection": 0.0}
            for slot, p in assignments
        ],
        "bench": sorted(
            [_player_line(p, projector, week, C.BENCH_SLOT) for p in bench]
            + [_player_line(p, projector, week, C.IR_SLOT)
               for p in team.roster if p.lineup_slot == C.IR_SLOT],
            key=lambda line: -line["projection"],
        ),
    }


# One week's matchup for a team: both best lineups and the win probability.
def matchup(league, team, projector, week):
    slots = league.settings.starting_slots()
    mine = best_lineup(team, slots, projector, week)
    opponent = league.opponent_for(team.team_id, week)
    result = {"week": week, "current_week": projector.current_week,
              "is_current": week == projector.current_week,
              "mine": mine, "opponent": None, "win_probability": None}
    if opponent:
        theirs = best_lineup(opponent, slots, projector, week)
        result["opponent"] = theirs
        result["win_probability"] = winprob.win_probability(
            mine["total"], theirs["total"], mine["stddev"], theirs["stddev"]
        )
    return result


# The rest of the season at a glance: every week's opponent and both
# projected totals, plus the weeks already played with their real scores.
def season_schedule(league, team, projector):
    slots = league.settings.starting_slots()
    weeks = sorted({m.week for m in league.matchups if team.team_id in
                    (m.home_team_id, m.away_team_id)})
    rows = []
    for week in weeks:
        match = league.matchup_for(team.team_id, week)
        opponent = league.team_by_id(match.opponent_of(team.team_id)) if match else None
        row = {"week": week, "opponent": opponent.name if opponent else None,
               "opponent_id": opponent.team_id if opponent else None,
               "played": week < projector.current_week}
        if row["played"] and match:
            mine_home = match.home_team_id == team.team_id
            row["my_score"] = match.home_score if mine_home else match.away_score
            row["their_score"] = match.away_score if mine_home else match.home_score
        elif opponent:
            mine = best_lineup(team, slots, projector, week)
            theirs = best_lineup(opponent, slots, projector, week)
            row["my_projection"] = mine["total"]
            row["their_projection"] = theirs["total"]
            row["win_probability"] = winprob.win_probability(
                mine["total"], theirs["total"], mine["stddev"], theirs["stddev"]
            )
        rows.append(row)
    return rows


# One player's season: past weeks (each source and the actual score, from
# the trends history) and every remaining week (each source, the blend, the
# NFL opponent), plus rest-of-season totals by source.
def player_outlook(player, projector, final_week, trend_history=None, owner=None):
    past = []
    for row in (trend_history or {}).get("weeks", []):
        if row["week"] < projector.current_week:
            past.append(row)

    future = []
    for week in range(projector.current_week, final_week + 1):
        future.append({
            "week": week,
            "projection": round(projector.points(player, week), 2),
            "sources": {k: round(v, 1) for k, v in projector.sources(player, week).items()},
            "game": projector.nfl_game(player, week),
            "on_bye": projector.on_bye(player, week),
        })

    return {
        "player_id": player.player_id,
        "name": player.name,
        "position": player.position,
        "pro_team": player.pro_team,
        "injury_status": C.INJURY_STATUS_NAMES.get(player.injury_status, player.injury_status),
        "owner": owner,
        "percent_owned": round(player.percent_owned, 1),
        "ros_points": round(player.ros_points, 1),
        "ros_games": player.ros_games,
        "ros_sources": {k: round(v, 1) for k, v in player.ros_source_points.items()},
        "past": past,
        "future": future,
        "ros_history": (trend_history or {}).get("ros", {}),
    }
