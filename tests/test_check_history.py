"""The history probe: which past seasons exist, and which endpoint answers."""

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import check_history
from ffopt.config import Settings
from ffopt.espn_client import EspnAuthError


SETTINGS = Settings(league_id="123456", season=2025, swid="{A}", espn_s2="x")

LEAGUE = {
    "settings": {"name": "The League"},
    "teams": [
        {"name": "Alpha", "record": {"overall": {"wins": 9}}},
        {"name": "Bravo", "rankCalculatedFinal": 1, "record": {"overall": {"wins": 7}}},
    ],
}


# Answer with a league for the years listed, 404 for the rest.
def transport_for(seasons_years, history_years=()):
    def handler(request):
        url = str(request.url)
        if "leagueHistory" in url:
            year = int(request.url.params["seasonId"])
            if year in history_years:
                return httpx.Response(200, json=[LEAGUE])
            return httpx.Response(404)
        for year in seasons_years:
            if f"/seasons/{year}/" in url:
                return httpx.Response(200, json=LEAGUE)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_found_season_reports_name_and_champion():
    result = check_history.probe_year(SETTINGS, 2025, transport_for([2025]))

    assert result["found"]
    assert result["endpoint"] == "seasons"
    assert result["name"] == "The League"
    assert result["top_team"] == "Bravo"


def test_season_missing_from_both_endpoints_is_not_found():
    result = check_history.probe_year(SETTINGS, 2022, transport_for([2025]))

    assert not result["found"]


def test_old_season_falls_back_to_history_endpoint():
    transport = transport_for([], history_years=[2015])
    result = check_history.probe_year(SETTINGS, 2015, transport)

    assert result["found"]
    assert result["endpoint"] == "leagueHistory"


# A league imported from another site keeps its recent seasons ONLY on the
# history endpoint, so a recent year must fall back too.
def test_recent_imported_season_is_found_on_the_history_endpoint():
    transport = transport_for([2026], history_years=[2023])
    result = check_history.probe_year(SETTINGS, 2023, transport)

    assert result["found"]
    assert result["endpoint"] == "leagueHistory"


def test_auth_failure_is_raised():
    transport = httpx.MockTransport(lambda request: httpx.Response(401))

    with pytest.raises(EspnAuthError):
        check_history.probe_year(SETTINGS, 2025, transport)


def test_verdict_reports_only_current_season():
    results = [(2025, {"found": True}), (2024, {"found": False})]

    assert "only 2025" in check_history.verdict(results)


def test_verdict_reports_the_range():
    results = [(2025, {"found": True}), (2023, {"found": True}), (2022, {"found": False})]

    assert "2023 to 2025" in check_history.verdict(results)
