"""ESPN's kept history: rules and final places saved, account ids left out."""

import json
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import fetch_espn_history as E
from ffopt.config import Settings


SETTINGS = Settings(league_id="1661209171", season=2026, swid="{SECRET-SWID}", espn_s2="secret-s2")


def make_payload(year):
    return {
        "id": 1661209171,
        "seasonId": year,
        "settings": {
            "name": "The League",
            "size": 2,
            "scoringSettings": {"scoringItems": [{"statId": 3, "points": 0.04}]},
            "rosterSettings": {"lineupSlotCounts": {"0": 1}},
            "tradeSettings": {"deadlineDate": 123},
            "somethingElse": "not a rule",
        },
        "teams": [
            {"id": 2, "name": "Bravo", "rankCalculatedFinal": 1, "rankFinal": 2, "waiverRank": 3,
             "transactionCounter": {"acquisitions": 4}, "owners": ["{PRIVATE-GUID-B}"]},
            {"id": 1, "name": "Alpha", "rankCalculatedFinal": 2, "rankFinal": 1, "waiverRank": 1,
             "transactionCounter": {"acquisitions": 9}, "owners": ["{PRIVATE-GUID-A}"]},
        ],
    }


def transport_for(years):
    def handler(request):
        year = int(request.url.params["seasonId"])
        if year in years:
            return httpx.Response(200, json=[make_payload(year)])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_missing_season_returns_none():
    with httpx.Client(transport=transport_for([2024])) as client:
        assert E.fetch_season(client, "1", 2019) is None


def test_settings_keep_the_rules_and_drop_everything_else():
    settings = E.build_settings(make_payload(2024))

    assert settings["season"] == 2024
    assert settings["scoringSettings"]["scoringItems"][0]["statId"] == 3
    assert "somethingElse" not in settings


def test_teams_are_sorted_and_carry_final_rank():
    teams = E.build_teams(make_payload(2024))

    assert [t["team_id"] for t in teams] == [1, 2]
    assert teams[1]["name"] == "Bravo"
    assert teams[1]["final_rank"] == 1
    assert teams[1]["transaction_count"] == {"acquisitions": 4}


def test_account_ids_and_cookies_never_reach_the_saved_files(tmp_path):
    code = E.main(
        ["--from", "2025", "--to", "2024", "--out", str(tmp_path)],
        transport=transport_for([2025, 2024]),
        settings=SETTINGS,
    )

    assert code == 0
    for path in tmp_path.rglob("*.json"):
        text = path.read_text()
        assert "PRIVATE-GUID" not in text
        assert "SECRET" not in text
        assert "secret-s2" not in text
    assert (tmp_path / "2024" / "espn_settings.json").exists()
    assert json.loads((tmp_path / "2025" / "espn_teams.json").read_text())[0]["name"] == "Alpha"


def test_main_reports_failure_when_nothing_is_found(tmp_path):
    code = E.main(
        ["--from", "2025", "--to", "2024", "--out", str(tmp_path)],
        transport=transport_for([]),
        settings=SETTINGS,
    )

    assert code == 1
