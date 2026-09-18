"""
The league's NFL.com-era history (2020-2025), shaped for the dashboard.

The raw record lives as JSON files under doc/league_history/, one folder per
season. This joins them into one payload: a row per owner per season, with the
standings, playoff result, waiver activity and trade count side by side, plus
the trades, the most-moved players and the draft recaps.

Owners are matched across years by owner_user_id, not by team name, so a
renamed team is still the same person.
"""

import json
from pathlib import Path

from .config import PROJECT_ROOT


HISTORY_DIR = PROJECT_ROOT / "doc" / "league_history"


def _read(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text())


# Every season folder, oldest first.
def season_years(root):
    years = [int(p.name) for p in Path(root).iterdir() if p.is_dir() and p.name.isdigit()]
    return sorted(years)


# One season, as a row per team with everything we know about it.
def load_season(root, year):
    folder = Path(root) / str(year)
    teams = {t["team_id"]: t for t in _read(folder / "teams.json", [])}
    rankings = _read(folder / "rankings.json", [])
    playoffs = _read(folder / "playoffs.json", {})
    activity = {a["team_id"]: a for a in _read(folder / "transactions_summary.json", [])}
    trades = _read(folder / "trades.json", [])
    playoff_by_team = {t["team_id"]: t for t in playoffs.get("teams", [])}

    trade_counts = {}
    for trade in trades:
        for side in ("side_a", "side_b"):
            team_id = trade[side].get("team_id")
            trade_counts[team_id] = trade_counts.get(team_id, 0) + 1

    rows = []
    for rank in rankings:
        team_id = rank["team_id"]
        team = teams.get(team_id, {})
        playoff = playoff_by_team.get(team_id, {})
        games = rank["wins"] + rank["losses"] + rank["ties"]
        rows.append(
            {
                "season": year,
                "team_id": team_id,
                "team_name": rank["name"],
                "owner_user_id": team.get("owner_user_id"),
                "owner_name": team.get("owner_name") or "Unknown",
                "rank": rank["rank"],
                "wins": rank["wins"],
                "losses": rank["losses"],
                "ties": rank["ties"],
                "games": games,
                "points_for": rank["points_for"],
                "points_against": rank["points_against"],
                "final_place": playoff.get("final_place"),
                "playoff_seed": playoff.get("playoff_seed"),
                "adds": (activity.get(team_id) or {}).get("adds", 0),
                "trades": trade_counts.get(team_id, 0),
            }
        )

    champion = (playoffs.get("champion") or {}).get("name")
    return {
        "season": year,
        "num_teams": len(rankings),
        "games": max((r["games"] for r in rows), default=0),
        "champion_team": champion,
        "rows": rows,
        "trades": [
            {
                "season": year,
                "date": trade["date"],
                "sides": [
                    {
                        "team": trade[side]["team"],
                        "owner_name": trade[side]["owner_name"],
                        "gives": [
                            f"{p['name']} ({p['position']})" for p in trade[side]["gives"]
                        ],
                    }
                    for side in ("side_a", "side_b")
                ],
            }
            for trade in trades
        ],
        "draft": _read(folder / "draft.json"),
    }


# Everything the dashboard needs, in one payload. None when there is no
# history on disk.
def load_history(root=HISTORY_DIR):
    root = Path(root)
    if not root.exists():
        return None
    years = season_years(root)
    if not years:
        return None

    seasons = [load_season(root, year) for year in years]
    all_time = _read(root / "all_time.json", {})

    # One owner list, ordered by wins, with their titles and seasons played.
    owners = {}
    for season in seasons:
        for row in season["rows"]:
            key = row["owner_user_id"] or row["owner_name"]
            owner = owners.setdefault(
                key,
                {
                    "owner_user_id": row["owner_user_id"],
                    "owner_name": row["owner_name"],
                    "titles": 0,
                    "team_names": [],
                },
            )
            if row["team_name"] not in owner["team_names"]:
                owner["team_names"].append(row["team_name"])
            if row["final_place"] == 1:
                owner["titles"] += 1

    players = _read(root / "players_all_time.json", [])
    most_moved = sorted(
        (
            {
                "player": p["player"],
                "position": p.get("position", ""),
                "moves": len(p["moves"]),
                "last_season": max(m["season"] for m in p["moves"]),
            }
            for p in players
            if len(p["moves"]) >= 2
        ),
        key=lambda p: (-p["moves"], p["player"]),
    )

    return {
        "league_name": (_read(root / str(years[-1]) / "league.json", {}) or {}).get("name"),
        "years": years,
        "champions": all_time.get("champions", []),
        "owners": list(owners.values()),
        "seasons": seasons,
        "most_moved_players": most_moved[:15],
        "drafts": [s["draft"] for s in seasons if s["draft"]],
    }
