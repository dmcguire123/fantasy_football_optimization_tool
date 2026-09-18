"""The NFL.com history fetcher: season ids, derived files, and all-time totals."""

import json
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetch_nfl_history as F


LEAGUE_ID = "9072865"


# A tiny two-team league for one year. Team 2 wins the title even though
# team 1 had the better regular season.
def make_payload(year, champion_id="2"):
    def team(team_id, name, owner, rank, place, wins, pts):
        return {
            "teamId": team_id,
            "name": name,
            "ownerUserId": owner,
            "coManagerUserId": None,
            "imageUrl": "http://logo",
            "stats": {
                "season": {
                    str(year): {
                        "rank": str(rank),
                        "divisionRank": str(rank),
                        "record": f"{wins}-{14 - wins}-0",
                        "wins": str(wins),
                        "losses": str(14 - wins),
                        "ties": "0",
                        "streak": "W1",
                        "pts": str(pts),
                        "ptsAgainst": "1500.50",
                        "playoffSeed": str(rank),
                        "playoffBracketType": "winners",
                        "place": str(place),
                        "transactionAddCount": 10,
                        "transactionTradeCount": 1,
                        "waiverPriority": str(rank),
                    }
                }
            },
        }

    other_id = "1" if champion_id == "2" else "2"
    return {
        "games": {
            F.game_id(year): {
                "leagues": {
                    LEAGUE_ID: {
                        "leagueId": LEAGUE_ID,
                        "name": "Test League",
                        "numTeams": 2,
                        "maxTeams": "2",
                        "isSeasonOver": True,
                        "finalStandingsTeamIds": [champion_id, other_id],
                        "divisions": {"1": {"name": "Div", "teamIds": ["1", "2"]}},
                        "teams": {
                            "1": team("1", f"Alpha {year}", "100", 1, 2 if champion_id == "2" else 1, 10, 1800.5),
                            "2": team("2", "Bravo", "200", 2, 1 if champion_id == "2" else 2, 8, 1700),
                        },
                    }
                }
            }
        },
        "users": {"100": {"name": "Ann"}, "200": {"name": "Bo"}},
    }


def transport_for(years):
    def handler(request):
        params = request.url.params
        if request.url.path.endswith("/league/teams"):
            return httpx.Response(200, json=make_payload(2025))
        for year in years:
            if params.get("gameId") == F.game_id(year):
                return httpx.Response(200, json=make_payload(year))
        return httpx.Response(400, json={"errors": []})

    return httpx.MockTransport(handler)


def test_game_id_is_ten_then_the_year():
    assert F.game_id(2024) == "102024"


def test_missing_season_returns_none():
    with httpx.Client(transport=transport_for([2025])) as client:
        assert F.fetch_standings(client, LEAGUE_ID, 2019) is None


def test_champion_comes_from_final_place_not_regular_season_rank():
    year = 2024
    payload = make_payload(year)
    league = F.find_league(payload, LEAGUE_ID, year)

    playoffs = F.build_playoffs(league, year)
    rankings = F.build_rankings(league, year)

    assert rankings[0]["name"] == "Alpha 2024"
    assert playoffs["champion"]["name"] == "Bravo"


def test_numbers_are_converted_from_strings():
    payload = make_payload(2024)
    league = F.find_league(payload, LEAGUE_ID, 2024)
    row = F.build_rankings(league, 2024)[0]

    assert row["wins"] == 10
    assert row["points_for"] == 1800.5
    assert row["points_against"] == 1500.5


def test_teams_carry_owner_names():
    payload = make_payload(2024)
    league = F.find_league(payload, LEAGUE_ID, 2024)

    names = {row["owner_name"] for row in F.build_teams(payload, league, 2024)}
    assert names == {"Ann", "Bo"}


def test_all_time_totals_follow_the_owner_across_seasons():
    seasons = {}
    for year, champion in [(2023, "1"), (2024, "2"), (2025, "2")]:
        payload = make_payload(year, champion_id=champion)
        seasons[year] = (payload, F.find_league(payload, LEAGUE_ID, year))

    result = F.build_all_time(seasons)
    owners = {o["owner_name"]: o for o in result["owners"]}

    assert owners["Bo"]["titles"] == 2
    assert owners["Ann"]["titles"] == 1
    assert owners["Ann"]["seasons"] == 3
    assert [c["season"] for c in result["champions"]] == [2023, 2024, 2025]


def test_main_writes_one_folder_per_season(tmp_path):
    out = tmp_path / "history"
    code = F.main(
        ["--league", LEAGUE_ID, "--from", "2025", "--to", "2022", "--out", str(out)],
        transport=transport_for([2025, 2024]),
    )

    assert code == 0
    assert sorted(p.name for p in out.iterdir() if p.is_dir()) == ["2024", "2025"]
    for name in ["league", "teams", "rankings", "playoffs", "transactions_summary"]:
        assert (out / "2025" / f"{name}.json").exists()
    assert (out / "2025" / "raw" / "standings.json").exists()
    assert json.loads((out / "all_time.json").read_text())["champions"]
