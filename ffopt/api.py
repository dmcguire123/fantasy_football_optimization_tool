"""
HTTP API and static UI host.

Every read endpoint works as soon as a league id and season are configured.
Endpoints that change your roster additionally need the two ESPN cookies and
a team id, and they refuse to do anything unless the caller explicitly turns
off dry run, so a mistyped request can never submit a real transaction.
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import constants as C
from . import history, scouting, waivers
from .config import PROJECT_ROOT, load_settings
from .espn_client import EspnError, build_lineup_payload
from .intel.service import IntelService
from .league import LeagueService
from .optimizer import optimize_team, season_projection, week_projection


WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(
    title="Fantasy Football Optimization Tool",
    description="Read, analyze, and manage an ESPN fantasy football team.",
    version="1.0.0",
)

# One service per process so the short-lived cache is actually shared.
_service = None


def get_service():
    global _service
    if _service is None:
        settings = load_settings()
        if not settings.is_configured:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "League is not configured yet.",
                    "missing": settings.missing_fields(),
                    "hint": "Copy .env.example to .env and fill in your league details.",
                },
            )
        _service = LeagueService(settings)
    return _service


# Intel is built on the league service so both share one ESPN connection.
_intel = None


def get_intel():
    global _intel
    service = get_service()
    if _intel is None or _intel.league_service is not service:
        _intel = IntelService(service)
    return _intel


def reset_service():
    """Drop the cached service, e.g. after configuration changes or in tests."""
    global _service, _intel
    _intel = None
    if _service is not None:
        try:
            _service.close()
        except Exception:
            pass
    _service = None


# Turn an ESPN failure into a response that tells the user what to fix,
# rather than a bare 500.
@app.exception_handler(EspnError)
def handle_espn_error(request, exc):
    return JSONResponse(status_code=502, content=exc.to_dict())


def require_writable(settings):
    if settings.read_only:
        raise HTTPException(
            status_code=403,
            detail="Running in read-only mode. Unset FFOPT_READ_ONLY to make moves.",
        )
    if not settings.has_credentials:
        raise HTTPException(
            status_code=403,
            detail="Roster moves need ESPN_SWID and ESPN_S2 to be set in .env.",
        )
    if not settings.team_id:
        raise HTTPException(
            status_code=400,
            detail="Set ESPN_TEAM_ID in .env so the app knows which team is yours.",
        )


# Resolve the team whose moves we are making, and fail clearly if we cannot.
def resolve_my_team(service, week=None):
    team = service.my_team(week)
    if not team:
        raise HTTPException(status_code=404, detail="No teams found in this league.")
    return team


class LineupMove(BaseModel):
    player_id: int
    from_slot: int
    to_slot: int


class ApplyLineupRequest(BaseModel):
    week: int | None = None
    moves: list[LineupMove] | None = Field(
        default=None,
        description="Explicit moves. Omit to apply the optimizer's recommendation.",
    )
    dry_run: bool = Field(
        default=True,
        description="Must be false to actually submit the change to ESPN.",
    )


class ClaimRequest(BaseModel):
    add_player_id: int
    drop_player_id: int | None = None
    bid_amount: float | None = Field(default=None, description="FAAB bid, if used.")
    is_waiver: bool = Field(
        default=True,
        description="True queues a waiver claim; false takes a free agent now.",
    )
    week: int | None = None
    dry_run: bool = True


# Settings for the league this process is serving, falling back to the
# environment before a service has been built.
def current_settings():
    if _service is not None:
        return _service.settings
    return load_settings()


@app.get("/api/config")
def read_config():
    """What the app knows about the league, with secrets redacted."""
    return current_settings().public_dict()


@app.get("/api/league")
def read_league(week: int | None = None):
    """League settings, standings, and the week being viewed."""
    service = get_service()
    league = service.load_league(week)
    my_team = service.my_team(week)

    return {
        "settings": league.settings.to_dict(),
        "week": league.week,
        "my_team_id": my_team.team_id if my_team else None,
        "teams": [t.to_dict(include_roster=False) for t in league.teams],
    }


@app.get("/api/teams/{team_id}")
def read_team(team_id: int, week: int | None = None):
    """One team with its full roster, so you can scout it player by player."""
    service = get_service()
    league = service.load_league(week)
    team = league.team_by_id(team_id)
    if not team:
        raise HTTPException(status_code=404, detail=f"No team with id {team_id}.")

    return {
        "week": league.week,
        "team": team.to_dict(),
        "lineup_efficiency": scouting.lineup_efficiency(
            team, league.settings.starting_slots()
        ),
    }


@app.get("/api/my-team")
def read_my_team(
    week: int | None = None,
    objective: str = Query("points", pattern="^(points|win)$"),
):
    """Your roster plus the optimal lineup and the moves to get there."""
    service = get_service()
    league = service.load_league(week)
    team = resolve_my_team(service, week)

    result = optimize_team(
        team,
        league.settings.starting_slots(),
        objective=objective,
        opponent=scouting.opponent_distribution(league, team, league.week),
    )

    return {
        "week": league.week,
        "team": team.to_dict(),
        "optimal": result.to_dict(),
    }


@app.get("/api/lineup/optimize")
def optimize_lineup(
    week: int | None = None,
    horizon: str = Query("week", pattern="^(week|season)$"),
    objective: str = Query("points", pattern="^(points|win)$"),
):
    """The best startable lineup, by this week or by rest-of-season value.

    With objective=win the lineup is tuned for the chance of beating this
    week's opponent rather than for raw projected points.
    """
    service = get_service()
    league = service.load_league(week)
    team = resolve_my_team(service, week)

    projection_fn = week_projection if horizon == "week" else season_projection
    opponent = scouting.opponent_distribution(league, team, league.week)
    result = optimize_team(
        team,
        league.settings.starting_slots(),
        projection_fn=projection_fn,
        objective=objective,
        opponent=opponent,
    )

    return {
        "week": league.week,
        "horizon": horizon,
        "objective": result.objective,
        "optimal": result.to_dict(),
    }


@app.post("/api/lineup/apply")
def apply_lineup(request: ApplyLineupRequest):
    """Submit lineup changes to ESPN. Requires dry_run to be false."""
    service = get_service()
    settings = service.settings
    require_writable(settings)

    league = service.load_league(request.week)
    team = resolve_my_team(service, request.week)

    if request.moves is not None:
        moves = [m.model_dump() for m in request.moves]
    else:
        result = optimize_team(team, league.settings.starting_slots())
        moves = result.moves

    if not moves:
        return {"submitted": False, "reason": "Lineup is already optimal.", "moves": []}

    payload = build_lineup_payload(
        team_id=team.team_id,
        member_id=settings.swid,
        week=league.week,
        moves=moves,
    )

    if request.dry_run:
        return {
            "submitted": False,
            "dry_run": True,
            "moves": moves,
            "payload": payload,
            "hint": "Send the same request with dry_run false to apply it.",
        }

    response = service.client.submit_transaction(payload)
    service.invalidate()
    return {"submitted": True, "moves": moves, "espn_response": response}


@app.get("/api/free-agents")
def read_free_agents(
    week: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    positions: str | None = Query(
        None, description="Comma-separated, e.g. RB,WR"
    ),
):
    """The available player pool, newest projections first."""
    service = get_service()
    league = service.load_league(week)
    position_list = [p.strip() for p in positions.split(",")] if positions else None

    players = service.free_agents(
        week=league.week, limit=limit, positions=position_list
    )
    players.sort(key=lambda p: p.effective_projection, reverse=True)

    return {
        "week": league.week,
        "count": len(players),
        "players": [p.to_dict() for p in players],
    }


@app.get("/api/projections/value")
def read_projection_value(
    week: int | None = None,
    pool_size: int = Query(300, ge=10, le=500),
    limit: int = Query(15, ge=1, le=50),
):
    """Players our model or the other sources rate above the experts or ESPN."""
    from .projections.value import value_report

    service = get_service()
    league = service.load_league(week)
    team = resolve_my_team(service, week)
    pool = service.free_agents(week=league.week, limit=pool_size)
    report = value_report(team.roster, pool, limit=limit)
    report["week"] = league.week
    report["projection_source"] = service.settings.projection_source
    return report


@app.get("/api/projections/trends")
def read_projection_trends(week: int | None = None, limit: int = Query(10, ge=1, le=50)):
    """Weekly and rest-of-season projection risers and fallers."""
    from .projections.trends import ros_movers, weekly_movers

    service = get_service()
    data = service.trends(week)
    if data is None:
        raise HTTPException(400, "Trends need FFOPT_PROJECTIONS=consensus.")
    team = resolve_my_team(service, week)
    mine = {str(p.player_id) for p in team.roster}
    players = [
        {"player_id": pid, "name": info.get("name"), "position": info.get("position"),
         "team": info.get("team"), "mine": pid in mine}
        for pid, info in data.players.items()
        if info.get("name") and data.week in data.weekly.get(pid, {})
    ]
    players.sort(key=lambda p: (not p["mine"], p["name"]))
    return {
        "week": data.week,
        "weekly": weekly_movers(data, limit=limit),
        "ros": ros_movers(data, limit=limit),
        "my_team": {
            "weekly": weekly_movers(data, limit=limit, only=mine),
            "ros": ros_movers(data, limit=limit, only=mine),
        },
        "players": players,
    }


@app.get("/api/projections/trends/{player_id}")
def read_player_trend(player_id: str, week: int | None = None):
    """One player's projections by source, week by week, and rest of season by day."""
    service = get_service()
    data = service.trends(week)
    if data is None:
        raise HTTPException(400, "Trends need FFOPT_PROJECTIONS=consensus.")
    if player_id not in data.weekly and player_id not in data.ros:
        raise HTTPException(404, "No projection history for that player.")
    return {"week": data.week, **data.player(player_id)}


@app.get("/api/dashboard/season")
def read_dashboard_season(team_id: int | None = None):
    """Your season week by week: each opponent, both projected totals, results so far."""
    from . import dashboard

    service = get_service()
    league = service.load_league()
    team = league.team_by_id(team_id) if team_id else resolve_my_team(service)
    if not team:
        raise HTTPException(404, "No such team.")
    projector = service.projector()
    return {
        "current_week": league.week,
        "final_week": league.settings.final_week,
        "team": {"team_id": team.team_id, "name": team.name, "record": team.record},
        "teams": [{"team_id": t.team_id, "name": t.name} for t in league.teams],
        "weeks": dashboard.season_schedule(league, team, projector),
    }


@app.get("/api/dashboard/matchup")
def read_dashboard_matchup(week: int | None = None, team_id: int | None = None):
    """Both teams' best lineups and projected totals for any week."""
    from . import dashboard

    service = get_service()
    league = service.load_league()
    team = league.team_by_id(team_id) if team_id else resolve_my_team(service)
    if not team:
        raise HTTPException(404, "No such team.")
    week = week or league.week
    if week < league.week or week > league.settings.final_week:
        raise HTTPException(400, f"Pick a week from {league.week} to {league.settings.final_week}.")
    return dashboard.matchup(league, team, service.projector(), week)


@app.get("/api/players/search")
def search_players(q: str = Query("", max_length=60), limit: int = Query(20, ge=1, le=100)):
    """Players on any roster or among the most-owned free agents, by name."""
    service = get_service()
    wanted = q.strip().lower()
    matches = []
    for player, owner in service.player_pool().values():
        if wanted and wanted not in player.name.lower():
            continue
        matches.append({
            "player_id": player.player_id, "name": player.name, "position": player.position,
            "pro_team": player.pro_team, "owner": owner,
            "projection": round(player.effective_projection, 1),
            "ros_points": round(player.ros_points, 1),
        })
    matches.sort(key=lambda m: -m["ros_points"])
    return {"players": matches[:limit]}


@app.get("/api/players/{player_id}/outlook")
def read_player_outlook(player_id: int):
    """One player's past weeks, every remaining week by source, and rest of season."""
    from . import dashboard

    service = get_service()
    league = service.load_league()
    entry = service.player_pool().get(player_id)
    if not entry:
        raise HTTPException(404, "That player isn't rostered or among the top free agents.")
    player, owner = entry
    trend_history = None
    try:
        data = service.trends()
        if data is not None:
            trend_history = data.player(player_id)
    except Exception:
        trend_history = None
    return dashboard.player_outlook(
        player, service.projector(), league.settings.final_week, trend_history, owner
    )


@app.get("/api/waivers/recommendations")
def read_waiver_recommendations(
    week: int | None = None,
    limit: int = Query(15, ge=1, le=50),
    pool_size: int = Query(120, ge=10, le=400),
    positions: str | None = None,
    objective: str = Query("points", pattern="^(points|win)$"),
):
    """Available players ranked by what they would add to your lineup."""
    service = get_service()
    league = service.load_league(week)
    team = resolve_my_team(service, week)

    position_list = [p.strip() for p in positions.split(",")] if positions else None
    pool = service.free_agents(week=league.week, limit=pool_size)

    weeks_left = max(1, league.settings.final_week - league.week + 1)
    recommendations = waivers.recommend_pickups(
        team,
        pool,
        league.settings.starting_slots(),
        roster_limit=league.settings.active_roster_size,
        faab_remaining=team.faab_remaining,
        weeks_left=weeks_left,
        limit=limit,
        positions=position_list,
        opponent=scouting.opponent_distribution(league, team, league.week),
        objective=objective,
    )

    return {
        "week": league.week,
        "uses_faab": league.settings.uses_faab,
        "faab_remaining": team.faab_remaining,
        "waiver_priority": team.waiver_priority,
        "weeks_left": weeks_left,
        "recommendations": [r.to_dict() for r in recommendations],
        "drop_candidates": waivers.drop_candidates(
            team, league.settings.starting_slots()
        ),
    }


@app.get("/api/waivers/consensus")
def read_waiver_consensus(week: int | None = None, refresh: bool = False):
    """Expert waiver picks scraped from the web, merged and ranked."""
    intel = get_intel()
    if refresh:
        intel.refresh()
    return intel.consensus(week)


@app.get("/api/waivers/rosters")
def read_waiver_rosters(week: int | None = None):
    """Every team's roster, what it is short on, and its FAAB."""
    return get_intel().rosters(week)


@app.get("/api/waivers/targets")
def read_waiver_targets(
    week: int | None = None, limit: int = Query(20, ge=1, le=50)
):
    """Targets for your team with a suggested bid as a percent of FAAB."""
    return get_intel().targets(week, limit=limit)


@app.get("/api/waivers/stash")
def read_waiver_stash(
    week: int | None = None, limit: int = Query(20, ge=1, le=50)
):
    """Bench stashes: injury cover, handcuffs, bye weeks and rival threat."""
    return get_intel().stash_picks(week, limit=limit)


@app.post("/api/waivers/claim")
def submit_claim(request: ClaimRequest):
    """Submit a waiver claim or a free agent add. Requires dry_run false."""
    service = get_service()
    settings = service.settings
    require_writable(settings)

    league = service.load_league(request.week)
    team = resolve_my_team(service, request.week)

    roster_limit = league.settings.active_roster_size
    if roster_limit and not request.drop_player_id:
        roster_count = len([p for p in team.roster if p.lineup_slot != C.IR_SLOT])
        if roster_count >= roster_limit:
            raise HTTPException(
                status_code=400,
                detail="Roster is full, so this claim needs a player to drop.",
            )

    payload = waivers.build_claim(
        team_id=team.team_id,
        member_id=settings.swid,
        week=league.week,
        add_player_id=request.add_player_id,
        drop_player_id=request.drop_player_id,
        bid_amount=request.bid_amount,
        is_waiver=request.is_waiver,
    )

    if request.dry_run:
        return {
            "submitted": False,
            "dry_run": True,
            "payload": payload,
            "hint": "Send the same request with dry_run false to submit it.",
        }

    response = service.client.submit_transaction(payload)
    service.invalidate()
    return {"submitted": True, "payload": payload, "espn_response": response}


@app.get("/api/transactions/pending")
def read_pending_transactions():
    """Waiver claims you have already submitted but that have not processed."""
    service = get_service()
    return {"pending": service.pending_transactions()}


@app.post("/api/history/snapshot")
def take_snapshot(backfill: bool = False):
    """Record this week's matchup predictions and settle finished games."""
    service = get_service()
    conn = history.open_db()
    try:
        return history.run_snapshot(service, conn, do_backfill=backfill)
    finally:
        conn.close()


@app.get("/api/history/calibration")
def read_calibration():
    """How well past win probabilities matched what actually happened."""
    conn = history.open_db()
    try:
        report = history.calibration(conn)
        report["pending"] = history.pending_count(conn)
        return report
    finally:
        conn.close()


@app.get("/api/scouting/power-rankings")
def read_power_rankings(week: int | None = None):
    """Every team ranked by season scoring and current roster strength."""
    service = get_service()
    league = service.load_league(week)
    return {
        "week": league.week,
        "rankings": scouting.power_rankings(league, league.settings.starting_slots()),
    }


@app.get("/api/scouting/opponent")
def read_opponent(week: int | None = None):
    """A scouting report on the team you are playing this week."""
    service = get_service()
    league = service.load_league(week)
    team = resolve_my_team(service, week)
    return scouting.opponent_report(
        league, team, league.settings.starting_slots(), week=league.week
    )


@app.get("/api/scouting/trade-targets")
def read_trade_targets(week: int | None = None):
    """Other teams' surplus players that would improve your starting lineup."""
    service = get_service()
    league = service.load_league(week)
    team = resolve_my_team(service, week)
    return {
        "week": league.week,
        "targets": scouting.trade_targets(
            league, team, league.settings.starting_slots()
        ),
    }


@app.get("/api/scouting/positional-surplus")
def read_positional_surplus(week: int | None = None):
    """Roster depth by position for every team in the league."""
    service = get_service()
    league = service.load_league(week)
    return {
        "week": league.week,
        "rows": scouting.positional_surplus(league, league.settings.starting_slots()),
    }


@app.get("/")
def serve_index():
    """The browser UI."""
    index = WEB_DIR / "index.html"
    if not index.exists():
        return {"message": "UI not installed. The API is at /docs."}
    return FileResponse(index)
