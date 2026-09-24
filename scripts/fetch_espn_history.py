"""
Save what ESPN kept of your league's past seasons.

A league imported from NFL.com keeps its old seasons on ESPN's leagueHistory
endpoint, but only as a shell: team names, final place, and the league rules.
There are no draft picks, matchups, rosters or transactions. This script saves
the parts that exist next to the NFL.com data in doc/league_history/<year>/:

    espn_settings.json   scoring, roster, trade, draft and schedule rules
    espn_teams.json      team name, final rank, waiver rank, transaction count

ESPN account ids (SWIDs) are deliberately not saved. Cookies come from .env.

    python scripts/fetch_espn_history.py
    python scripts/fetch_espn_history.py --from 2025 --to 2020
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx

from ffopt.config import default_season, load_settings
from ffopt.espn_client import READ_HOST


HISTORY_PATH = "/apis/v3/games/ffl/leagueHistory/{league_id}"
DEFAULT_OUT = os.path.join(os.path.dirname(__file__), "..", "doc", "league_history")

# Which parts of ESPN's settings block are league RULES, worth keeping.
RULE_KEYS = [
    "name",
    "size",
    "scoringSettings",
    "rosterSettings",
    "tradeSettings",
    "draftSettings",
    "acquisitionSettings",
    "scheduleSettings",
    "restrictionType",
    "isPublic",
]


# One season from the history endpoint, or None if ESPN has nothing for it.
def fetch_season(client, league_id, year):
    url = READ_HOST + HISTORY_PATH.format(league_id=league_id)
    params = [("seasonId", year), ("view", "mSettings"), ("view", "mTeam")]

    response = client.get(url, params=params)
    if response.status_code in (400, 404):
        return None
    response.raise_for_status()

    payload = response.json()
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not payload or not payload.get("teams"):
        return None
    return payload


# The league rules for that season, without account ids or team data.
def build_settings(payload):
    settings = payload.get("settings") or {}
    rules = {key: settings[key] for key in RULE_KEYS if key in settings}
    return {"season": payload.get("seasonId"), "league_id": payload.get("id"), **rules}


# Team rows with the fields that hold real information. Owner ids are left
# out on purpose: they are ESPN account identifiers.
def build_teams(payload):
    rows = []
    for team in payload.get("teams") or []:
        rows.append(
            {
                "team_id": team.get("id"),
                "name": team.get("name"),
                "final_rank": team.get("rankCalculatedFinal"),
                "regular_season_final_rank": team.get("rankFinal"),
                "playoff_seed": team.get("playoffSeed"),
                "waiver_rank": team.get("waiverRank"),
                "transaction_count": team.get("transactionCounter"),
                "owner_count": len(team.get("owners") or []),
            }
        )
    return sorted(rows, key=lambda row: row["team_id"] or 0)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def save_season(out_dir, payload, year):
    year_dir = os.path.join(out_dir, str(year))
    write_json(os.path.join(year_dir, "espn_settings.json"), build_settings(payload))
    write_json(os.path.join(year_dir, "espn_teams.json"), build_teams(payload))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--from", dest="start", type=int, default=default_season())
    parser.add_argument("--to", dest="stop", type=int, default=2020)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--env-file", default=None, help="path to a .env with the ESPN cookies")
    return parser.parse_args(argv)


def main(argv=None, transport=None, settings=None):
    args = parse_args(argv)
    if settings is None:
        settings = load_settings(env_file=args.env_file) if args.env_file else load_settings()

    if not settings.league_id:
        print("ESPN_LEAGUE_ID is not set. Fill in .env first.")
        return 1

    cookies = {}
    if settings.swid:
        cookies["SWID"] = settings.swid
    if settings.espn_s2:
        cookies["espn_s2"] = settings.espn_s2

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0 Safari/537.36",
        "X-Fantasy-Source": "kona",
        "X-Fantasy-Platform": "kona-PROD--1",
    }

    out_dir = os.path.abspath(args.out)
    saved = 0
    with httpx.Client(cookies=cookies, headers=headers, timeout=30, transport=transport) as client:
        for year in range(args.start, args.stop - 1, -1):
            payload = fetch_season(client, settings.league_id, year)
            if payload is None:
                print(f"{year}: nothing on the history endpoint")
                continue
            save_season(out_dir, payload, year)
            saved += 1
            print(f"{year}: saved {len(payload['teams'])} teams and the league rules")

    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
