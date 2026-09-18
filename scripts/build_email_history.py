"""
Turn the trade emails saved in doc/league_history/source into per-year files.

Reads doc/league_history/source/gmail_trade_processed.json and writes, for
each season that has trades:

    doc/league_history/<year>/trades.json    each completed trade, side by side
    doc/league_history/<year>/players.json   every player move, oldest first

plus doc/league_history/players_all_time.json, one entry per player with every
trade they were part of.

Team names in the emails are matched to team ids using each year's teams.json
(which comes from the NFL API), so run fetch_nfl_history.py first.

    python scripts/build_email_history.py
"""

import argparse
import json
import os
import re
import sys


HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "doc", "league_history")

# One player in the email text looks like "A. St. BrownWR - DET". Players are
# run together with no separator, so the next player's initial sits right
# after the team, for example "T. McLaurinWR - WASW. RobinsonWR - NYG". The
# lazy team match plus the lookahead splits "WAS" from "W. Robinson".
PLAYER_PATTERN = re.compile(
    r"(?P<name>[A-Z][A-Za-z'\-]*\. [A-Za-z'\-\.\s]+?)"
    r"(?P<position>QB|RB|WR|TE|K|DEF) - "
    r"(?P<team>[A-Z]{2,3}?)"
    r"(?=[A-Z]\. |$)"
)


# Split one side's text into players. Returns None if the pieces do not put
# back together into exactly the original text, so a bad parse is never
# written out quietly.
def parse_players(text):
    players = []
    rebuilt = ""

    for match in PLAYER_PATTERN.finditer(text):
        players.append(
            {
                "name": match.group("name").strip(),
                "position": match.group("position"),
                "nfl_team": match.group("team"),
            }
        )
        rebuilt += match.group(0)

    if rebuilt != text:
        return None
    return players


def read_json(path):
    with open(path) as handle:
        return json.load(handle)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


# The team list NFL's API gave us for one season, or an empty list.
def load_year_teams(history_dir, year):
    path = os.path.join(history_dir, str(year), "teams.json")
    if not os.path.exists(path):
        return []
    return read_json(path)


# Match an email team name to a team that year. NFL.com cuts long names and
# adds "...", and the API sometimes has two spaces where the email has one.
def find_team(teams, email_name):
    wanted = " ".join(email_name.replace("...", "").split()).lower()
    truncated = email_name.endswith("...")

    for team in teams:
        actual = " ".join((team["name"] or "").split()).lower()
        if truncated and actual.startswith(wanted):
            return team
        if not truncated and actual == wanted:
            return team
    return None


def build_side(teams, side):
    team = find_team(teams, side["team"])
    players = parse_players(side["gives"])
    return {
        "team": side["team"],
        "team_id": team["team_id"] if team else None,
        "owner_name": team["owner_name"] if team else None,
        "gives": players,
        "gives_raw": side["gives"],
    }


# One trade record. The year comes from the email date.
def build_trade(teams, trade):
    return {
        "date": trade["date"],
        "message_id": trade["message_id"],
        "side_a": build_side(teams, trade["side_a"]),
        "side_b": build_side(teams, trade["side_b"]),
    }


# One row per player who changed teams: who gave him up and who got him.
def build_moves(trades):
    moves = []
    for trade in trades:
        for giver, receiver in [
            (trade["side_a"], trade["side_b"]),
            (trade["side_b"], trade["side_a"]),
        ]:
            for player in giver["gives"] or []:
                moves.append(
                    {
                        "date": trade["date"],
                        "player": player["name"],
                        "position": player["position"],
                        "nfl_team": player["nfl_team"],
                        "from_team": giver["team"],
                        "to_team": receiver["team"],
                        "message_id": trade["message_id"],
                    }
                )
    return sorted(moves, key=lambda move: move["date"])


# Group every move by player across all seasons.
def build_player_index(moves_by_year):
    index = {}
    for year, moves in moves_by_year.items():
        for move in moves:
            key = f"{move['player']} ({move['position']})"
            entry = index.setdefault(key, {"player": move["player"], "position": move["position"], "moves": []})
            entry["moves"].append({"season": year, **move})

    ordered = sorted(index.values(), key=lambda e: (-len(e["moves"]), e["player"]))
    for entry in ordered:
        entry["moves"].sort(key=lambda move: move["date"])
    return ordered


# Join each draft recap to how that team actually finished, so the grade and
# projection can be read next to the result.
def build_draft(history_dir, recap, team_name):
    year = recap["season"]
    draft = dict(recap)

    playoffs_path = os.path.join(history_dir, str(year), "playoffs.json")
    rankings_path = os.path.join(history_dir, str(year), "rankings.json")
    if not (os.path.exists(playoffs_path) and os.path.exists(rankings_path)):
        return draft

    teams = load_year_teams(history_dir, year)
    team = find_team(teams, team_name)
    if team is None:
        return draft

    playoffs = read_json(playoffs_path)
    rankings = read_json(rankings_path)
    final = [t for t in playoffs["teams"] if t["team_id"] == team["team_id"]]
    ranked = [t for t in rankings if t["team_id"] == team["team_id"]]

    draft["actual"] = {
        "team_id": team["team_id"],
        "regular_season_rank": ranked[0]["rank"] if ranked else None,
        "record": ranked[0]["record"] if ranked else None,
        "final_place": final[0]["final_place"] if final else None,
    }
    return draft


def write_drafts(history_dir):
    path = os.path.join(history_dir, "source", "gmail_draft_recaps.json")
    if not os.path.exists(path):
        return
    source = read_json(path)

    for recap in source["recaps"]:
        draft = build_draft(history_dir, recap, source["team"])
        write_json(os.path.join(history_dir, str(recap["season"]), "draft.json"), draft)
        print(f"{recap['season']}: draft recap written")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--dir", default=HISTORY_DIR)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    history_dir = os.path.abspath(args.dir)
    source = read_json(os.path.join(history_dir, "source", "gmail_trade_processed.json"))

    trades_by_year = {}
    for raw in source["trades"]:
        year = int(raw["date"][:4])
        teams = load_year_teams(history_dir, year)
        trades_by_year.setdefault(year, []).append(build_trade(teams, raw))

    problems = 0
    moves_by_year = {}

    for year in sorted(trades_by_year):
        trades = sorted(trades_by_year[year], key=lambda t: t["date"])
        moves = build_moves(trades)
        moves_by_year[year] = moves

        for trade in trades:
            for side in ("side_a", "side_b"):
                if trade[side]["gives"] is None:
                    problems += 1
                    print(f"{year}: could not parse {trade[side]['gives_raw']!r}")
                if trade[side]["team_id"] is None:
                    print(f"{year}: no team match for {trade[side]['team']!r} (kept by name only)")

        write_json(os.path.join(history_dir, str(year), "trades.json"), trades)
        write_json(os.path.join(history_dir, str(year), "players.json"), moves)
        print(f"{year}: {len(trades)} trades, {len(moves)} player moves")

    write_json(os.path.join(history_dir, "players_all_time.json"), build_player_index(moves_by_year))
    write_drafts(history_dir)

    if problems:
        print(f"{problems} sides failed to parse")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
