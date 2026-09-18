"""The HTTP layer: headers, cookies, filters, errors, and write payloads."""

import httpx
import pytest
from conftest import make_transport

from ffopt import constants as C
from ffopt.config import Settings
from ffopt.espn_client import (
    EspnAuthError,
    EspnClient,
    EspnError,
    EspnNotFoundError,
    build_add_drop_payload,
    build_lineup_payload,
)


def test_requests_carry_the_league_cookies(client, requests_log):
    client.fetch_league_snapshot()
    request = requests_log[-1]

    assert "SWID=" in request.headers.get("cookie", "")
    assert "espn_s2=" in request.headers.get("cookie", "")


def test_reads_go_to_the_read_host_with_the_right_views(client, requests_log):
    client.fetch_league_snapshot(week=5)
    url = str(requests_log[-1].url)

    assert "lm-api-reads.fantasy.espn.com" in url
    assert "/seasons/2025/segments/0/leagues/123456" in url
    assert "view=mRoster" in url
    assert "scoringPeriodId=5" in url


def test_free_agent_query_sends_a_fantasy_filter(client, requests_log):
    client.fetch_free_agents(week=5, limit=25, slot_ids=[2, 4])
    request = requests_log[-1]

    assert "kona_player_info" in str(request.url)

    import json
    sent = json.loads(request.headers["x-fantasy-filter"])["players"]
    assert sent["limit"] == 25
    assert sent["filterSlotIds"]["value"] == [2, 4]
    assert C.STATUS_FREEAGENT in sent["filterStatus"]["value"]


def test_writes_go_to_the_write_host(client, requests_log):
    payload = build_lineup_payload(1, "{ME}", 5, [
        {"player_id": 102, "from_slot": 20, "to_slot": 2}
    ])
    client.submit_transaction(payload)
    request = requests_log[-1]

    assert request.method == "POST"
    assert "lm-api-writes.fantasy.espn.com" in str(request.url)
    assert str(request.url).endswith("/transactions/")


def build_client(settings, status_code, body=None, text=None):
    def handler(request):
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=body or {})

    return EspnClient(settings, transport=httpx.MockTransport(handler))


def test_a_401_explains_the_cookie_problem(settings):
    espn = build_client(settings, 401, {"message": "nope"})
    with pytest.raises(EspnAuthError) as caught:
        espn.fetch_league_snapshot()

    assert "ESPN_SWID" in caught.value.message
    assert caught.value.status_code == 401


def test_a_404_explains_the_league_id_problem(settings):
    espn = build_client(settings, 404)
    with pytest.raises(EspnNotFoundError) as caught:
        espn.fetch_league_snapshot()

    assert "123456" in caught.value.message
    assert "2025" in caught.value.message


def test_an_html_login_page_is_reported_as_an_expired_cookie(settings):
    espn = build_client(settings, 200, text="<html>Log in</html>")
    with pytest.raises(EspnError) as caught:
        espn.fetch_league_snapshot()

    assert "expired" in caught.value.message


def test_read_only_mode_blocks_writes(settings):
    settings.read_only = True
    espn = EspnClient(settings, transport=make_transport())

    with pytest.raises(EspnError) as caught:
        espn.submit_transaction({"type": "ROSTER"})

    assert "read-only" in caught.value.message.lower()


def test_writes_without_cookies_are_refused():
    settings = Settings(league_id="1", season=2025, team_id=1)
    espn = EspnClient(settings, transport=make_transport())

    with pytest.raises(EspnAuthError):
        espn.submit_transaction({"type": "ROSTER"})


def test_bye_weeks_come_back_keyed_by_pro_team(client):
    byes = client.fetch_bye_weeks()

    assert byes[9] == 5
    assert byes[12] == 10


def test_a_bye_week_lookup_failure_is_not_fatal(settings):
    espn = build_client(settings, 500)
    assert espn.fetch_bye_weeks() == {}


def test_lineup_payload_lists_every_move_as_one_transaction():
    payload = build_lineup_payload(4, "{ME}", 7, [
        {"player_id": 1, "from_slot": 20, "to_slot": 2},
        {"player_id": 2, "from_slot": 2, "to_slot": 20},
    ])

    assert payload["type"] == "ROSTER"
    assert payload["executionType"] == "EXECUTE"
    assert payload["teamId"] == 4
    assert payload["memberId"] == "{ME}"
    assert payload["scoringPeriodId"] == 7
    assert len(payload["items"]) == 2
    assert all(item["type"] == "LINEUP" for item in payload["items"])
    assert payload["items"][0]["fromLineupSlotId"] == 20
    assert payload["items"][0]["toLineupSlotId"] == 2


def test_an_empty_add_drop_is_rejected():
    with pytest.raises(ValueError):
        build_add_drop_payload(1, "{ME}", 5)


def test_a_drop_only_transaction_is_allowed():
    payload = build_add_drop_payload(1, "{ME}", 5, drop_player_id=99)

    assert len(payload["items"]) == 1
    assert payload["items"][0]["type"] == "DROP"


def test_the_cache_stops_repeat_requests(settings, requests_log):
    settings.cache_ttl_seconds = 60.0
    from ffopt.league import LeagueService

    espn = EspnClient(settings, transport=make_transport(requests_log))
    service = LeagueService(settings, client=espn)

    service.load_league()
    first_count = len(requests_log)
    service.load_league()

    assert len(requests_log) == first_count

    service.invalidate()
    service.load_league()
    assert len(requests_log) > first_count
