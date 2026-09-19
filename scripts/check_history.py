"""
Check which past seasons of your league exist on ESPN.

Read-only. Tries each year from the current season back to --to and prints
what ESPN says about it. Use it to find out whether a league imported from
another site (like NFL.com) kept any history.

    python scripts/check_history.py
    python scripts/check_history.py --from 2025 --to 2015
"""

import argparse
import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ffopt.config import default_season, load_settings
from ffopt.espn_client import (
    READ_HOST,
    EspnAuthError,
    EspnClient,
    EspnError,
    EspnNotFoundError,
)


# ESPN serves past seasons from a separate endpoint. Seasons that were
# imported from another site (like NFL.com) live ONLY there, whatever the
# year, so it is tried for every season the normal endpoint does not have.
HISTORY_PATH = "/apis/v3/games/ffl/leagueHistory/{league_id}"


# Pick the champion (or best record) out of a list of ESPN teams.
def find_top_team(teams):
    if not teams:
        return ""

    best = None
    for team in teams:
        if team.get("rankCalculatedFinal") == 1:
            best = team
            break

    if best is None:
        best = max(teams, key=lambda t: _wins(t))

    return _team_name(best)


def _wins(team):
    overall = (team.get("record") or {}).get("overall") or {}
    return overall.get("wins", 0)


def _team_name(team):
    name = team.get("name")
    if name:
        return name
    return f"{team.get('location', '')} {team.get('nickname', '')}".strip()


# Boil a league payload down to the few fields we print.
def summarize(payload, endpoint):
    settings = payload.get("settings") or {}
    teams = payload.get("teams") or []
    return {
        "found": True,
        "endpoint": endpoint,
        "name": settings.get("name", ""),
        "teams": len(teams),
        "top_team": find_top_team(teams),
        "note": "",
    }


def missing(note):
    return {
        "found": False,
        "endpoint": "",
        "name": "",
        "teams": 0,
        "top_team": "",
        "note": note,
    }


# Older seasons live at leagueHistory and come back as a list of one league.
def fetch_history_endpoint(client, year):
    league_id = client.settings.league_id
    url = READ_HOST + HISTORY_PATH.format(league_id=league_id)
    params = [("seasonId", year), ("view", "mSettings"), ("view", "mTeam")]

    response = client._client.get(url, params=params)
    client._raise_for_status(response, url)

    payload = response.json()
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return payload


# Ask ESPN about one season. Returns a result dict; raises only on auth
# problems, because every other year will fail the same way.
def probe_year(settings, year, transport=None):
    year_settings = dataclasses.replace(settings, season=year)

    with EspnClient(year_settings, transport=transport) as client:
        try:
            payload = client.get_league(["mSettings", "mTeam"])
            return summarize(payload, "seasons")
        except EspnNotFoundError:
            pass

        try:
            payload = fetch_history_endpoint(client, year)
        except EspnNotFoundError:
            return missing("no league that year")
        except EspnAuthError:
            raise
        except EspnError as exc:
            return missing(f"error: {exc.message[:60]}")

        if not payload:
            return missing("no league that year")
        return summarize(payload, "leagueHistory")


# Turn the results into the table and the one-line verdict.
def format_report(results):
    lines = []
    lines.append(f"{'year':<6}{'found':<7}{'endpoint':<15}{'teams':<7}{'name / top team'}")

    for year, result in results:
        if result["found"]:
            detail = f"{result['name']} / {result['top_team']}"
            lines.append(
                f"{year:<6}{'yes':<7}{result['endpoint']:<15}"
                f"{result['teams']:<7}{detail}"
            )
        else:
            lines.append(f"{year:<6}{'no':<7}{'-':<15}{'-':<7}{result['note']}")

    lines.append("")
    lines.append(verdict(results))
    return "\n".join(lines)


def verdict(results):
    years = sorted(year for year, result in results if result["found"])
    if not years:
        return "No seasons found. Check ESPN_LEAGUE_ID and your cookies."
    if len(years) == 1:
        return f"No history: only {years[0]} exists on ESPN."
    return f"History found for {len(years)} seasons: {years[0]} to {years[-1]}."


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--from", dest="start", type=int, default=default_season())
    parser.add_argument("--to", dest="stop", type=int, default=2010)
    return parser.parse_args(argv)


def main(argv=None, transport=None):
    args = parse_args(argv)
    settings = load_settings()

    if not settings.league_id:
        print("ESPN_LEAGUE_ID is not set. Fill in .env first.")
        return 1

    results = []
    for year in range(args.start, args.stop - 1, -1):
        try:
            result = probe_year(settings, year, transport=transport)
        except EspnAuthError as exc:
            print(f"Stopped at {year}: {exc.message}")
            return 1
        results.append((year, result))

    print(format_report(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
