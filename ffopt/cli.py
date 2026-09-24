"""
Command line interface.

The same operations the web UI exposes, for when a terminal is faster.
Anything that changes your roster asks for confirmation unless --yes is
passed, so nothing is submitted to ESPN by accident.
"""

import argparse
import sys

from . import constants as C
from . import history, scouting, waivers
from .config import load_settings
from .espn_client import EspnError, build_lineup_payload
from .league import LeagueService
from .optimizer import optimize_team


# Print a table without pulling in a dependency for it.
def print_table(headers, rows):
    if not rows:
        print("  (nothing to show)")
        return

    widths = [len(h) for h in headers]
    text_rows = []
    for row in rows:
        cells = [str(c) for c in row]
        text_rows.append(cells)
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))

    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print("  " + header_line)
    print("  " + "  ".join("-" * w for w in widths))
    for cells in text_rows:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)))


def build_service(args):
    settings = load_settings()

    if not settings.is_configured:
        missing = ", ".join(settings.missing_fields())
        print(f"League is not configured. Missing: {missing}")
        print("Copy .env.example to .env and fill in your league details.")
        raise SystemExit(2)

    return LeagueService(settings)


# Ask before doing anything that reaches ESPN's write endpoint.
def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def cmd_info(args):
    service = build_service(args)
    league = service.load_league(args.week)
    team = service.my_team(args.week)

    print(f"League: {league.settings.name} ({league.settings.size} teams)")
    print(f"Season: {service.settings.season}   Week: {league.week}")
    print(f"Scoring: {league.settings.scoring_type}")
    slots = ", ".join(C.slot_name(s) for s in league.settings.starting_slots())
    print(f"Starting lineup: {slots}")
    if league.settings.uses_faab:
        print(f"Waivers: FAAB, ${league.settings.faab_budget:.0f} budget")
    else:
        print("Waivers: rolling priority")

    if team:
        print(f"\nYour team: {team.name} (id {team.team_id}), {team.record}")
        print(f"Projected this week, as set: {team.projected_starting_points:.2f}")
        if league.settings.uses_faab:
            print(f"FAAB remaining: ${team.faab_remaining:.0f}")
    return 0


def cmd_teams(args):
    service = build_service(args)
    league = service.load_league(args.week)
    rankings = scouting.power_rankings(league, league.settings.starting_slots())

    print(f"Power rankings, week {league.week}\n")
    rows = []
    for row in rankings:
        team = row["team"]
        rows.append(
            [
                row["power_rank"],
                team["name"][:24],
                team["record"],
                f"{row['points_per_game']:.1f}",
                f"{row['roster_strength_this_week']:.1f}",
                f"{row['power_score']:.1f}",
            ]
        )
    print_table(["#", "Team", "Rec", "PPG", "BestNow", "Power"], rows)
    return 0


def cmd_roster(args):
    service = build_service(args)
    league = service.load_league(args.week)

    if args.team_id:
        team = league.team_by_id(args.team_id)
    else:
        team = service.my_team(args.week)

    if not team:
        print("Team not found.")
        return 1

    print(f"{team.name} ({team.record}), week {league.week}\n")
    rows = []
    for player in sorted(team.roster, key=lambda p: (not p.is_starting, p.lineup_slot)):
        rows.append(
            [
                player.lineup_slot_name,
                player.name[:24],
                player.position,
                player.pro_team,
                f"{player.projected_points:.1f}",
                "BYE" if player.on_bye else C.INJURY_STATUS_NAMES.get(
                    player.injury_status, player.injury_status
                ),
            ]
        )
    print_table(["Slot", "Player", "Pos", "Team", "Proj", "Status"], rows)

    efficiency = scouting.lineup_efficiency(team, league.settings.starting_slots())
    print(
        f"\n  Projected as set: {efficiency['current_projected']:.2f}"
        f"   Best available: {efficiency['best_projected']:.2f}"
        f"   Left on bench: {efficiency['points_left_on_bench']:.2f}"
    )
    return 0


def cmd_lineup(args):
    service = build_service(args)
    league = service.load_league(args.week)
    team = service.my_team(args.week)
    if not team:
        print("Could not find your team.")
        return 1

    result = optimize_team(
        team,
        league.settings.starting_slots(),
        objective=args.objective,
        opponent=scouting.opponent_distribution(league, team, league.week),
    )

    print(f"Optimal lineup for {team.name}, week {league.week}\n")
    rows = []
    for slot_id, player in result.assignments:
        if player:
            rows.append(
                [
                    C.slot_name(slot_id),
                    player.name[:24],
                    player.pro_team,
                    f"{player.effective_projection:.1f}",
                ]
            )
        else:
            rows.append([C.slot_name(slot_id), "(empty)", "-", "0.0"])
    print_table(["Slot", "Player", "Team", "Proj"], rows)

    print(
        f"\n  Optimal total: {result.total:.2f}"
        f"   Current total: {result.current_total:.2f}"
        f"   Gain: {result.points_gained:+.2f}"
    )

    if result.win_probability is not None:
        print(
            f"  Win probability: {result.win_probability * 100:.1f}%"
            f"   Currently set: {result.current_win_probability * 100:.1f}%"
        )

    if not result.moves:
        print("\n  Your lineup is already optimal.")
        return 0

    print("\n  Moves needed:")
    for move in result.moves:
        print(
            f"    {move['player_name']}: "
            f"{move['from_slot_name']} -> {move['to_slot_name']}"
        )
    return 0


def cmd_apply_lineup(args):
    service = build_service(args)
    settings = service.settings

    if not settings.can_write:
        print(
            "Cannot submit moves. Need ESPN_SWID, ESPN_S2, and ESPN_TEAM_ID set, "
            "and FFOPT_READ_ONLY unset."
        )
        return 2

    league = service.load_league(args.week)
    team = service.my_team(args.week)
    result = optimize_team(team, league.settings.starting_slots())

    if not result.moves:
        print("Lineup is already optimal. Nothing to submit.")
        return 0

    print(f"About to submit {len(result.moves)} lineup change(s) for {team.name}:")
    for move in result.moves:
        print(
            f"  {move['player_name']}: "
            f"{move['from_slot_name']} -> {move['to_slot_name']}"
        )
    print(f"Projected gain: {result.points_gained:+.2f} points")

    if not confirm("Submit to ESPN?", args.yes):
        print("Cancelled.")
        return 0

    payload = build_lineup_payload(
        team_id=team.team_id,
        member_id=settings.swid,
        week=league.week,
        moves=result.moves,
    )
    service.client.submit_transaction(payload)
    service.invalidate()
    print("Submitted.")
    return 0


def cmd_waivers(args):
    service = build_service(args)
    league = service.load_league(args.week)
    team = service.my_team(args.week)
    if not team:
        print("Could not find your team.")
        return 1

    pool = service.free_agents(week=league.week, limit=args.pool)
    weeks_left = max(1, league.settings.final_week - league.week + 1)

    recommendations = waivers.recommend_pickups(
        team,
        pool,
        league.settings.starting_slots(),
        roster_limit=league.settings.roster_size,
        faab_remaining=team.faab_remaining,
        weeks_left=weeks_left,
        limit=args.limit,
        positions=args.positions.split(",") if args.positions else None,
        opponent=scouting.opponent_distribution(league, team, league.week),
        objective=args.objective,
    )

    print(f"Waiver targets for {team.name}, week {league.week}")
    if league.settings.uses_faab:
        print(f"FAAB remaining: ${team.faab_remaining:.0f}\n")
    else:
        print(f"Waiver priority: {team.waiver_priority}\n")

    rows = []
    for rec in recommendations:
        rows.append(
            [
                rec.player.name[:22],
                rec.player.position,
                rec.player.pro_team,
                f"{rec.player.effective_projection:.1f}",
                f"{rec.weekly_gain:+.2f}",
                f"{rec.season_gain:+.1f}",
                rec.drop_player.name[:20] if rec.drop_player else "(open spot)",
                f"${rec.suggested_bid:.0f}" if league.settings.uses_faab else "-",
                str(rec.player.player_id),
            ]
        )
    print_table(
        ["Player", "Pos", "Tm", "Proj", "WkGain", "SznGain", "Drop", "Bid", "Id"],
        rows,
    )
    print("\n  Use: ffopt claim --add <Id> --drop <Id> --bid <amount>")
    return 0


def cmd_claim(args):
    service = build_service(args)
    settings = service.settings

    if not settings.can_write:
        print(
            "Cannot submit claims. Need ESPN_SWID, ESPN_S2, and ESPN_TEAM_ID set, "
            "and FFOPT_READ_ONLY unset."
        )
        return 2

    league = service.load_league(args.week)
    team = service.my_team(args.week)

    kind = "free agent add" if args.free_agent else "waiver claim"
    print(f"About to submit a {kind} for {team.name}, week {league.week}:")
    print(f"  Add player id:  {args.add}")
    if args.drop:
        print(f"  Drop player id: {args.drop}")
    if args.bid is not None and not args.free_agent:
        print(f"  FAAB bid:       ${args.bid:.0f}")

    if not confirm("Submit to ESPN?", args.yes):
        print("Cancelled.")
        return 0

    payload = waivers.build_claim(
        team_id=team.team_id,
        member_id=settings.swid,
        week=league.week,
        add_player_id=args.add,
        drop_player_id=args.drop,
        bid_amount=args.bid,
        is_waiver=not args.free_agent,
    )
    service.client.submit_transaction(payload)
    service.invalidate()
    print("Submitted.")
    return 0


def cmd_snapshot(args):
    service = build_service(args)
    conn = history.open_db()
    result = history.run_snapshot(service, conn, do_backfill=args.backfill)

    print(f"Week {result['week']}: recorded {result['predicted']} matchup prediction(s).")
    if args.backfill:
        print(f"Backfilled {result['backfilled']} prediction(s) from earlier weeks.")
    print(f"Settled {result['settled']} finished game(s).")
    return 0


# Pull every projection source for the week, archive the pull, and show
# where the sources disagree with ESPN. Run it a few times a week so the
# archive builds up for grading sources later.
def cmd_projections(args):
    service = build_service(args)
    projection_service = service.projection_service()
    if not projection_service:
        print("Consensus projections are off. Set FFOPT_PROJECTIONS=consensus in .env.")
        return 1

    league = service.load_league(args.week)
    team = service.my_team(args.week)
    free_agents = service.free_agents(args.week, limit=args.pool)

    everyone = [p for t in league.teams for p in t.roster] + list(free_agents)
    saved = projection_service.archive_espn(service.settings.season, league.week, everyone)
    print(f"Week {league.week}: archived {saved} ESPN projection(s) to data/projections.db.")

    sources = ["espn", "sleeper", "fantasypros", "model"]

    def source_cells(player):
        return [
            f"{player.source_points[s]:.1f}" if s in player.source_points else "-"
            for s in sources
        ]

    if team:
        print(f"\n{team.name}, week {league.week}")
        rows = []
        for player in sorted(team.roster, key=lambda p: -p.consensus_points):
            rows.append(
                [player.name, player.position]
                + source_cells(player)
                + [f"{player.consensus_points:.1f}", f"{player.ros_points:.1f}"]
            )
        print_table(["Player", "Pos"] + sources + ["Blend", "ROS"], rows)

    gaps = [p for p in free_agents if len(p.source_points) > 1]
    gaps.sort(key=lambda p: -(p.consensus_points - p.espn_projected_points))
    print("\nAvailable players the other sources like more than ESPN does")
    rows = []
    for player in gaps[: args.limit]:
        rows.append(
            [player.name, player.position, player.pro_team]
            + source_cells(player)
            + [f"{player.consensus_points - player.espn_projected_points:+.1f}"]
        )
    print_table(["Player", "Pos", "Team"] + sources + ["vs ESPN"], rows)
    return 0


# Grade every projection source and our model on past seasons, and decide
# how much the model counts in the live blend. Slow the first time (it
# downloads several seasons of data); cached after that.
def cmd_backtest(args):
    from .projections import backtest
    from .projections.scoring import LeagueScoring

    settings = load_settings()
    scoring = LeagueScoring.default_ppr()
    if settings.is_configured:
        league = LeagueService(settings).load_league()
        scoring = LeagueScoring.from_settings(league.settings.scoring_settings)
        print(f"Scoring every source with {league.settings.name}'s rules.")
    else:
        print("No league configured; scoring with plain full PPR.")

    seasons = range(args.first_season, args.last_season + 1)
    print(f"Backtesting {seasons.start}-{seasons.stop - 1}. This takes a minute or two.")
    result = backtest.run(scoring, seasons)
    path = backtest.save_results(result)

    for position in backtest.POSITIONS + ("ALL",):
        print(f"\n{position}")
        rows = []
        for row in result["table"].filter(result["table"]["position"] == position).to_dicts():
            mae = f"{row['mae']:.2f}" if row.get("mae") is not None else ""
            rows.append([row["method"], mae, f"{row['spearman']:.3f}", row["n"]])
        print_table(["Method", "MAE", "Rank corr", "Games"], rows)

    print("\nModel weight in the live blend (0 = kept out):")
    for position in backtest.POSITIONS:
        status = "promoted" if result["promoted"][position] else "not promoted"
        print(f"  {position}: {result['weights'][position]:.2f} ({status})")
    print(f"\nSaved to {path}")
    return 0


# Who is undervalued this week: players our model rates above the experts
# (as expected points of edge, calibrated by the backtest), and players the
# other sources rate well above ESPN, which is what the rest of the league
# is looking at.
def cmd_value(args):
    from .projections.value import value_report

    service = build_service(args)
    league = service.load_league(args.week)
    team = service.my_team(args.week)
    pool = service.free_agents(args.week, limit=args.pool)
    report = value_report(team.roster, pool, limit=args.limit)

    backtest_info = report["backtest"]
    if not backtest_info["slopes"]:
        print("No backtest results yet, so the model's edge cannot be sized.")
        print("Run `python -m ffopt backtest` once to fix that.\n")

    def edge_rows(lines):
        return [
            [row["name"], row["position"], row["pro_team"], f"{row['experts']:.1f}",
             f"{row['model']:.1f}", f"{row['expected_edge']:+.1f}"]
            for row in lines
        ]

    headers = ["Player", "Pos", "Team", "Experts", "Model", "Edge"]
    print(f"Week {league.week}: available players our model likes more than the experts")
    print_table(headers, edge_rows(report["model_pickups"]))

    print("\nAvailable players the other sources rate well above ESPN")
    print_table(
        ["Player", "Pos", "Team", "ESPN", "Others", "Gap"],
        [
            [row["name"], row["position"], row["pro_team"], f"{row['espn']:.1f}",
             f"{row['others']:.1f}", f"{row['gap_vs_espn']:+.1f}"]
            for row in report["espn_behind"]
        ],
    )

    print(f"\n{team.name}: players the model is worried about")
    print_table(headers, edge_rows(report["roster_warnings"]))
    print(f"\n{team.name}: players the model likes more than the experts")
    print_table(headers, edge_rows(report["roster_boosts"]))
    print(
        "\nEdge is the expected points above the experts' number, sized by how much "
        "of the model's disagreement came true in the backtest."
    )
    return 0


# Everything worth knowing today as one JSON document, for the morning
# email (see .claude/skills/morning-report) or any other summary.
def cmd_report(args):
    import json

    from .report import build_report

    service = build_service(args)
    print(json.dumps(build_report(service, week=args.week), indent=2, default=str))
    return 0


# How projections have moved: weekly and rest-of-season risers and fallers,
# or one player's week-by-week history by source.
def cmd_trends(args):
    from .projections.trends import ros_movers, weekly_movers

    service = build_service(args)
    data = service.trends(args.week)
    if data is None:
        print("Trends need the consensus. Set FFOPT_PROJECTIONS=consensus in .env.")
        return 1

    if args.player:
        wanted = args.player.lower()
        matches = [pid for pid, info in data.players.items()
                   if wanted in (info.get("name") or "").lower()]
        if not matches:
            print(f"No player matching '{args.player}'.")
            return 1
        history = data.player(matches[0])
        print(f"{history.get('name')} ({history.get('position')}, {history.get('team')})\n")
        sources = ["espn", "sleeper", "fantasypros", "model", "consensus", "actual"]
        rows = [
            [row["week"]] + [f"{row[s]:.1f}" if s in row else "-" for s in sources]
            for row in history["weeks"]
        ]
        print_table(["Week"] + sources, rows)
        if history["ros"]:
            print("\nRest of season, by day pulled")
            days = sorted({p["date"] for series in history["ros"].values() for p in series})
            names = sorted(history["ros"])
            lookup = {(s, p["date"]): p["points"] for s, series in history["ros"].items()
                      for p in series}
            print_table(["Date"] + names,
                        [[d] + [f"{lookup[(s, d)]:.1f}" if (s, d) in lookup else "-"
                                for s in names] for d in days])
        return 0

    def mover_rows(moves):
        return [[m.get("name", m["player_id"]), m.get("position", ""), m.get("team", ""),
                 f"{m['before']:.1f}", f"{m['now']:.1f}", f"{m['change']:+.1f}"] for m in moves]

    headers = ["Player", "Pos", "Team", "Before", "Now", "Change"]
    weekly = weekly_movers(data, limit=args.limit)
    print(f"Week {data.week} projection vs his earlier weeks: risers")
    print_table(headers, mover_rows(weekly["risers"]))
    print("\nFallers")
    print_table(headers, mover_rows(weekly["fallers"]))

    ros = ros_movers(data, limit=args.limit)
    print("\nRest-of-season blend, change over the last week: risers")
    if ros["days_of_history"] < 2:
        print("  (rest-of-season history starts with the first archived pull; "
              "check back after a few days)")
    else:
        print_table(headers, mover_rows(ros["risers"]))
        print("\nFallers")
        print_table(headers, mover_rows(ros["fallers"]))
    return 0


def cmd_calibration(args):
    conn = history.open_db()
    report = history.calibration(conn)

    if not report["games"]:
        print("No finished games recorded yet. Run `snapshot` each week, or add --backfill.")
        print(f"({history.pending_count(conn)} prediction(s) waiting on results.)")
        return 0

    print(f"{report['games']} finished game(s). Lower scores are better; a coin flip is")
    print("0.250 (Brier) and 0.693 (log loss).\n")
    print_table(
        ["", "Brier", "Log loss"],
        [
            ["Measured spreads", f"{report['brier']:.3f}", f"{report['log_loss']:.3f}"],
            ["Fixed spread", f"{report['brier_flat']:.3f}", f"{report['log_loss_flat']:.3f}"],
        ],
    )
    print("\nWhen the model said...        it actually happened")
    rows = [
        [
            f"{b['low'] * 100:.0f}-{b['high'] * 100:.0f}%",
            str(b["count"]),
            f"{b['predicted'] * 100:.1f}%",
            f"{b['actual'] * 100:.1f}%",
        ]
        for b in report["bins"]
    ]
    print_table(["Band", "Sides", "Predicted", "Actual"], rows)
    return 0


def cmd_scout(args):
    service = build_service(args)
    league = service.load_league(args.week)
    team = service.my_team(args.week)
    report = scouting.opponent_report(
        league, team, league.settings.starting_slots(), week=league.week
    )

    if not report.get("has_opponent"):
        print(report.get("message", "No opponent this week."))
        return 0

    opponent = report["opponent"]
    print(f"Week {report['week']}: {team.name} vs {opponent['name']} ({opponent['record']})\n")
    print(f"  Your best lineup:     {report['my_projected_best']:.2f}")
    print(f"  Your lineup as set:   {report['my_projected_current']:.2f}")
    print(f"  Their lineup as set:  {report['their_projected_current']:.2f}")
    print(f"  Their best possible:  {report['their_projected_best']:.2f}")
    print(f"  Projected margin:     {report['projected_margin']:+.2f}")
    print(f"  Win probability:      {report['win_probability_if_optimal'] * 100:.1f}%")

    print("\n  Position by position (negative means they have the edge):")
    rows = [
        [row["slot_name"], f"{row['my_points']:.1f}", f"{row['their_points']:.1f}", f"{row['edge']:+.1f}"]
        for row in report["positional_edges"]
    ]
    print_table(["Slot", "You", "Them", "Edge"], rows)

    if report["their_risks"]:
        print("\n  Their questionable starters:")
        for player in report["their_risks"]:
            flag = "BYE" if player["on_bye"] else player["injury_status"]
            print(f"    {player['name']} ({player['position']}) - {flag}")
    return 0


def cmd_trades(args):
    service = build_service(args)
    league = service.load_league(args.week)
    team = service.my_team(args.week)
    targets = scouting.trade_targets(league, team, league.settings.starting_slots())

    if not targets:
        print("No clear trade targets found.")
        return 0

    print(f"Trade targets for {team.name}, week {league.week}\n")
    for entry in targets[: args.limit]:
        surplus = ", ".join(entry["their_surplus_positions"]) or "none"
        print(f"  {entry['team']['name']} ({entry['team']['record']}) - surplus at {surplus}")

        print("    Ask for:")
        for match in entry["players"]:
            player = match["player"]
            role = "their starter" if match["is_their_starter"] else "their bench"
            spare = ", they can spare it" if match["they_can_spare"] else ""
            print(
                f"      {player['name']} ({player['position']}, {player['pro_team']}, {role}{spare})"
                f"  +{match['weekly_gain_for_me']:.2f} to your lineup"
            )

        if entry["could_offer"]:
            print("    Could offer:")
            for offer in entry["could_offer"]:
                player = offer["player"]
                print(
                    f"      {player['name']} ({player['position']}, {player['pro_team']})"
                    f"  +{offer['weekly_gain_for_them']:.2f} to theirs"
                )
        print()
    return 0


def cmd_serve(args):
    import uvicorn

    settings = load_settings()
    if not settings.is_configured:
        missing = ", ".join(settings.missing_fields())
        print(f"Warning: league not configured yet (missing {missing}).")
        print("The UI will load, but it will ask you to fill in .env first.\n")

    print(f"Open http://{args.host}:{args.port} in your browser.")
    uvicorn.run("ffopt.api:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ffopt",
        description="Manage and optimize an ESPN fantasy football team.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub):
        sub.add_argument("--week", type=int, default=None, help="Week to act on.")
        return sub

    add_common(subparsers.add_parser("info", help="League and team summary."))
    add_common(subparsers.add_parser("teams", help="Power rankings for the league."))

    roster = add_common(subparsers.add_parser("roster", help="Show a roster."))
    roster.add_argument("--team-id", type=int, default=None, help="Defaults to your team.")

    lineup_cmd = add_common(subparsers.add_parser("lineup", help="Show the optimal lineup."))
    lineup_cmd.add_argument(
        "--objective",
        choices=["points", "win"],
        default="points",
        help="Maximize projected points, or the chance of beating this week's opponent.",
    )

    apply_lineup = add_common(
        subparsers.add_parser("apply-lineup", help="Submit the optimal lineup to ESPN.")
    )
    apply_lineup.add_argument("--yes", action="store_true", help="Skip confirmation.")

    waiver_cmd = add_common(
        subparsers.add_parser("waivers", help="Ranked waiver wire targets.")
    )
    waiver_cmd.add_argument("--limit", type=int, default=15)
    waiver_cmd.add_argument("--pool", type=int, default=120, help="Players to evaluate.")
    waiver_cmd.add_argument("--positions", default=None, help="e.g. RB,WR")
    waiver_cmd.add_argument(
        "--objective", choices=["points", "win"], default="points",
        help="Rank by points added, or by change in win probability.",
    )

    claim = add_common(subparsers.add_parser("claim", help="Submit a waiver claim."))
    claim.add_argument("--add", type=int, required=True, help="Player id to add.")
    claim.add_argument("--drop", type=int, default=None, help="Player id to drop.")
    claim.add_argument("--bid", type=float, default=None, help="FAAB bid amount.")
    claim.add_argument(
        "--free-agent",
        action="store_true",
        help="Add immediately instead of queuing a waiver claim.",
    )
    claim.add_argument("--yes", action="store_true", help="Skip confirmation.")

    add_common(subparsers.add_parser("scout", help="Scout this week's opponent."))

    snap = subparsers.add_parser(
        "snapshot", help="Record this week's matchup predictions and settle finished games."
    )
    snap.add_argument(
        "--backfill", action="store_true", help="Also rebuild predictions for earlier weeks."
    )
    subparsers.add_parser("calibration", help="How well win probabilities have held up.")

    value_cmd = add_common(
        subparsers.add_parser("value", help="Undervalued players this week.")
    )
    value_cmd.add_argument("--pool", type=int, default=300, help="Free agents to include.")
    value_cmd.add_argument("--limit", type=int, default=15)

    add_common(
        subparsers.add_parser("report", help="Today's report as JSON (for the morning email).")
    )

    trends_cmd = add_common(
        subparsers.add_parser("trends", help="Projection risers and fallers, or one player's history.")
    )
    trends_cmd.add_argument("--player", default=None, help="Part of a player's name.")
    trends_cmd.add_argument("--limit", type=int, default=10)

    backtest_cmd = subparsers.add_parser(
        "backtest", help="Grade ESPN, Sleeper, and our model on past seasons."
    )
    backtest_cmd.add_argument("--first-season", type=int, default=2018)
    backtest_cmd.add_argument("--last-season", type=int, default=2025)

    projections_cmd = add_common(
        subparsers.add_parser(
            "projections", help="Pull and archive every projection source, and compare them."
        )
    )
    projections_cmd.add_argument("--pool", type=int, default=300, help="Free agents to include.")
    projections_cmd.add_argument("--limit", type=int, default=15)

    trades = add_common(subparsers.add_parser("trades", help="Find trade targets."))
    trades.add_argument("--limit", type=int, default=5)

    serve = subparsers.add_parser("serve", help="Run the web app.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(week=None)

    return parser


COMMANDS = {
    "info": cmd_info,
    "teams": cmd_teams,
    "roster": cmd_roster,
    "lineup": cmd_lineup,
    "apply-lineup": cmd_apply_lineup,
    "waivers": cmd_waivers,
    "claim": cmd_claim,
    "scout": cmd_scout,
    "snapshot": cmd_snapshot,
    "calibration": cmd_calibration,
    "projections": cmd_projections,
    "backtest": cmd_backtest,
    "value": cmd_value,
    "report": cmd_report,
    "trends": cmd_trends,
    "trades": cmd_trades,
    "serve": cmd_serve,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    handler = COMMANDS.get(args.command)
    if not handler:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except EspnError as exc:
        print(f"\nESPN error: {exc.message}")
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
