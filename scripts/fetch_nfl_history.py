"""
Save your old NFL.com league history to disk.

NFL.com retired its fantasy site, but api.fantasy.nfl.com/v2 still answers for
two endpoints (standings and teams). This script fetches every season it can,
keeps the raw responses, and splits them into one JSON file per topic.

    python scripts/fetch_nfl_history.py --league 9072865
    python scripts/fetch_nfl_history.py --league 9072865 --from 2025 --to 2020

Output goes to doc/league_history/<year>/ plus an all_time.json at the top.
"""

import argparse
import json
import os
import sys

import httpx


API_HOST = "https://api.fantasy.nfl.com/v2"
DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "..", "doc", "league_history")

# NFL names each season's game 10<year>, so 2024 is 102024.
def game_id(year):
    return f"10{year}"


# One league, one season, straight from the API. A 400 means the league did
# not exist that year, so return None instead of raising.
def fetch_standings(client, league_id, year):
    url = f"{API_HOST}/league/standings"
    params = {"leagueId": league_id, "gameId": game_id(year)}

    response = client.get(url, params=params)
    if response.status_code in (400, 404, 500):
        return None
    response.raise_for_status()
    return response.json()


# The teams endpoint ignores gameId and always answers with the latest season.
def fetch_teams(client, league_id):
    url = f"{API_HOST}/league/teams"
    response = client.get(url, params={"leagueId": league_id})
    response.raise_for_status()
    return response.json()


# Pull the league object for one year out of a standings payload.
def find_league(payload, league_id, year):
    games = payload.get("games") or {}
    game = games.get(game_id(year))
    if not game:
        return None
    return (game.get("leagues") or {}).get(str(league_id))


# NFL sends every number as a string. Turn them into numbers where we can.
def to_number(value):
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value
    if as_float.is_integer():
        return int(as_float)
    return as_float


def season_stats(team, year):
    return ((team.get("stats") or {}).get("season") or {}).get(str(year)) or {}


def owner_name(payload, team):
    users = payload.get("users") or {}
    user = users.get(str(team.get("ownerUserId"))) or {}
    return user.get("name")


# One row per team with who owns it and what it was called that year.
def build_teams(payload, league, year):
    rows = []
    for team_id, team in league["teams"].items():
        rows.append(
            {
                "team_id": to_number(team_id),
                "name": team.get("name"),
                "owner_user_id": team.get("ownerUserId"),
                "owner_name": owner_name(payload, team),
                "co_manager_user_id": team.get("coManagerUserId"),
                "logo_url": team.get("imageUrl"),
            }
        )
    return sorted(rows, key=lambda row: row["team_id"])


# Regular season results, best rank first.
def build_rankings(league, year):
    rows = []
    for team_id, team in league["teams"].items():
        stats = season_stats(team, year)
        rows.append(
            {
                "team_id": to_number(team_id),
                "name": team.get("name"),
                "rank": to_number(stats.get("rank")),
                "division_rank": to_number(stats.get("divisionRank")),
                "record": stats.get("record"),
                "wins": to_number(stats.get("wins")),
                "losses": to_number(stats.get("losses")),
                "ties": to_number(stats.get("ties")),
                "streak": stats.get("streak"),
                "points_for": to_number(stats.get("pts")),
                "points_against": to_number(stats.get("ptsAgainst")),
            }
        )
    return sorted(rows, key=lambda row: row["rank"] or 999)


# Playoff seeding and where each team finished. Place 1 is the champion.
def build_playoffs(league, year):
    teams = []
    for team_id, team in league["teams"].items():
        stats = season_stats(team, year)
        teams.append(
            {
                "team_id": to_number(team_id),
                "name": team.get("name"),
                "playoff_seed": to_number(stats.get("playoffSeed")),
                "bracket": stats.get("playoffBracketType"),
                "final_place": to_number(stats.get("place")),
            }
        )
    teams.sort(key=lambda row: row["final_place"] or 999)

    champion = teams[0] if teams and teams[0]["final_place"] == 1 else None
    return {
        "season": year,
        "season_over": league.get("isSeasonOver"),
        "champion": champion,
        "final_standings_team_ids": [
            to_number(team_id) for team_id in league.get("finalStandingsTeamIds") or []
        ],
        "teams": teams,
    }


# How busy each team was: adds, trades, waiver priority.
def build_transactions_summary(league, year):
    rows = []
    for team_id, team in league["teams"].items():
        stats = season_stats(team, year)
        rows.append(
            {
                "team_id": to_number(team_id),
                "name": team.get("name"),
                "adds": to_number(stats.get("transactionAddCount")),
                "trades": to_number(stats.get("transactionTradeCount")),
                "waiver_priority": to_number(stats.get("waiverPriority")),
                "waiver_budget_remaining": to_number(stats.get("waiverBudgetRemaining")),
            }
        )
    return sorted(rows, key=lambda row: row["team_id"])


# League level facts: size, draft, divisions.
def build_league(league, year):
    divisions = []
    for division_id, division in (league.get("divisions") or {}).items():
        divisions.append(
            {
                "division_id": to_number(division_id),
                "name": division.get("name"),
                "team_ids": [to_number(t) for t in division.get("teamIds") or []],
            }
        )
    return {
        "league_id": league.get("leagueId"),
        "season": year,
        "name": league.get("name"),
        "league_type": league.get("leagueType"),
        "num_teams": league.get("numTeams"),
        "max_teams": to_number(league.get("maxTeams")),
        "owner_user_id": league.get("ownerUserId"),
        "manager_user_ids": league.get("leagueManagerUserIds"),
        "draft_type": league.get("draftType"),
        "draft_status": league.get("draftStatus"),
        "draft_date": league.get("draftDateTime"),
        "divisions": divisions,
    }


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
        handle.write("\n")


# Write the raw response and every derived file for one season.
def save_season(out_dir, payload, league, year):
    year_dir = os.path.join(out_dir, str(year))

    write_json(os.path.join(year_dir, "raw", "standings.json"), payload)
    write_json(os.path.join(year_dir, "league.json"), build_league(league, year))
    write_json(os.path.join(year_dir, "teams.json"), build_teams(payload, league, year))
    write_json(os.path.join(year_dir, "rankings.json"), build_rankings(league, year))
    write_json(os.path.join(year_dir, "playoffs.json"), build_playoffs(league, year))
    write_json(
        os.path.join(year_dir, "transactions_summary.json"),
        build_transactions_summary(league, year),
    )


# Roll every season into one record per owner, keyed by owner user id, so a
# person is the same person even when they rename their team.
def build_all_time(seasons):
    champions = []
    owners = {}

    for year in sorted(seasons):
        payload, league = seasons[year]
        teams = build_teams(payload, league, year)
        rankings = build_rankings(league, year)
        playoffs = build_playoffs(league, year)

        owner_by_team = {row["team_id"]: row for row in teams}

        champion = playoffs["champion"]
        if champion:
            owner = owner_by_team[champion["team_id"]]
            champions.append(
                {
                    "season": year,
                    "team_name": champion["name"],
                    "owner_name": owner["owner_name"],
                }
            )

        for row in rankings:
            owner = owner_by_team[row["team_id"]]
            key = owner["owner_user_id"]
            entry = owners.setdefault(
                key,
                {
                    "owner_user_id": key,
                    "owner_name": owner["owner_name"],
                    "seasons": 0,
                    "wins": 0,
                    "losses": 0,
                    "ties": 0,
                    "points_for": 0,
                    "points_against": 0,
                    "titles": 0,
                    "team_names": [],
                },
            )
            entry["seasons"] += 1
            entry["wins"] += row["wins"] or 0
            entry["losses"] += row["losses"] or 0
            entry["ties"] += row["ties"] or 0
            entry["points_for"] = round(entry["points_for"] + (row["points_for"] or 0), 2)
            entry["points_against"] = round(
                entry["points_against"] + (row["points_against"] or 0), 2
            )
            if row["name"] not in entry["team_names"]:
                entry["team_names"].append(row["name"])

        if champion:
            owners[owner_by_team[champion["team_id"]]["owner_user_id"]]["titles"] += 1

    ranked = sorted(owners.values(), key=lambda o: (-o["titles"], -o["wins"]))
    return {"champions": champions, "owners": ranked}


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--league", required=True, help="NFL.com league id")
    parser.add_argument("--from", dest="start", type=int, default=2025)
    parser.add_argument("--to", dest="stop", type=int, default=2010)
    parser.add_argument("--out", default=DEFAULT_OUT)
    return parser.parse_args(argv)


def main(argv=None, transport=None):
    args = parse_args(argv)
    out_dir = os.path.abspath(args.out)

    seasons = {}
    with httpx.Client(timeout=20, transport=transport) as client:
        for year in range(args.start, args.stop - 1, -1):
            payload = fetch_standings(client, args.league, year)
            league = find_league(payload, args.league, year) if payload else None

            if not league:
                print(f"{year}: no league")
                continue

            save_season(out_dir, payload, league, year)
            seasons[year] = (payload, league)
            print(f"{year}: saved {len(league['teams'])} teams")

        if not seasons:
            print("No seasons found. Check the league id.")
            return 1

        teams_payload = fetch_teams(client, args.league)
        write_json(os.path.join(out_dir, "current_teams_raw.json"), teams_payload)

    write_json(os.path.join(out_dir, "all_time.json"), build_all_time(seasons))
    print(f"Wrote {len(seasons)} seasons to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
