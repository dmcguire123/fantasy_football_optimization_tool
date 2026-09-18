"""The HTTP API, driven against the mocked ESPN fixtures."""

import pytest
from conftest import make_transport
from fastapi.testclient import TestClient

from ffopt import api
from ffopt.espn_client import EspnClient
from ffopt.league import LeagueService


@pytest.fixture
def http(settings, requests_log):
    espn = EspnClient(settings, transport=make_transport(requests_log))
    api._service = LeagueService(settings, client=espn)
    with TestClient(api.app) as test_client:
        yield test_client
    api._service = None


# Requests that actually reached ESPN's write endpoint.
def write_requests(log):
    return [r for r in log if r.method == "POST"]


def test_config_reports_readiness_without_leaking_cookies(http):
    body = http.get("/api/config").json()

    assert body["missing_fields"] == []
    assert "espn_s2" not in body
    assert "swid" not in body


def test_league_endpoint_lists_teams(http):
    body = http.get("/api/league").json()

    assert body["settings"]["name"] == "Test Gridiron League"
    assert body["week"] == 5
    assert body["my_team_id"] == 1
    assert len(body["teams"]) == 3
    assert "roster" not in body["teams"][0]


def test_my_team_includes_the_optimal_lineup(http):
    body = http.get("/api/my-team").json()

    assert body["team"]["team_id"] == 1
    assert len(body["optimal"]["lineup"]) == 9
    assert body["optimal"]["points_gained"] > 0
    assert body["optimal"]["moves"]


def test_any_team_roster_can_be_scouted(http):
    body = http.get("/api/teams/2").json()

    assert body["team"]["name"] == "Rival Crew"
    assert len(body["team"]["roster"]) == 11
    assert body["lineup_efficiency"]["points_left_on_bench"] == 0.0


def test_an_unknown_team_is_a_404(http):
    assert http.get("/api/teams/99").status_code == 404


def test_optimize_accepts_a_season_horizon(http):
    weekly = http.get("/api/lineup/optimize?horizon=week").json()
    season = http.get("/api/lineup/optimize?horizon=season").json()

    assert weekly["horizon"] == "week"
    assert season["horizon"] == "season"
    assert len(season["optimal"]["lineup"]) == 9


def test_optimize_supports_the_win_objective(http):
    body = http.get("/api/lineup/optimize?objective=win").json()

    assert len(body["optimal"]["lineup"]) == 9
    assert body["objective"] in ("win", "points")
    if body["objective"] == "win":
        assert 0.0 <= body["optimal"]["win_probability"] <= 1.0


def test_an_invalid_objective_is_rejected(http):
    assert http.get("/api/lineup/optimize?objective=vibes").status_code == 422


def test_an_invalid_horizon_is_rejected(http):
    assert http.get("/api/lineup/optimize?horizon=decade").status_code == 422


def test_dry_run_lineup_apply_never_posts_to_espn(http, requests_log):
    response = http.post("/api/lineup/apply", json={"dry_run": True})
    body = response.json()

    assert body["submitted"] is False
    assert body["dry_run"] is True
    assert body["payload"]["type"] == "ROSTER"
    assert write_requests(requests_log) == []


def test_apply_defaults_to_dry_run_when_not_specified(http, requests_log):
    body = http.post("/api/lineup/apply", json={}).json()

    assert body["submitted"] is False
    assert write_requests(requests_log) == []


def test_applying_for_real_posts_the_optimizer_moves(http, requests_log):
    body = http.post("/api/lineup/apply", json={"dry_run": False}).json()

    assert body["submitted"] is True
    assert body["moves"]
    assert len(write_requests(requests_log)) == 1

    import json
    sent = json.loads(write_requests(requests_log)[0].content)
    assert sent["type"] == "ROSTER"
    assert sent["teamId"] == 1
    assert sent["items"]


def test_explicit_moves_are_sent_verbatim(http, requests_log):
    moves = [{"player_id": 102, "from_slot": 20, "to_slot": 2}]
    body = http.post(
        "/api/lineup/apply", json={"moves": moves, "dry_run": False}
    ).json()

    assert body["submitted"] is True
    assert len(body["moves"]) == 1

    import json
    sent = json.loads(write_requests(requests_log)[0].content)
    assert sent["items"][0]["playerId"] == 102
    assert sent["items"][0]["toLineupSlotId"] == 2


def test_free_agents_are_listed_best_first(http):
    body = http.get("/api/free-agents?limit=20").json()
    projections = [p["effective_projection"] for p in body["players"]]

    assert body["count"] == 4
    assert projections == sorted(projections, reverse=True)


def test_waiver_recommendations_include_bids_and_drops(http):
    body = http.get("/api/waivers/recommendations?limit=5").json()

    assert body["uses_faab"] is True
    assert body["faab_remaining"] == 75.0
    assert body["recommendations"]
    assert body["drop_candidates"]

    top = body["recommendations"][0]
    assert top["weekly_gain"] > 0
    assert top["suggested_bid"] >= 1


def test_dry_run_claim_never_posts_to_espn(http, requests_log):
    body = http.post(
        "/api/waivers/claim",
        json={"add_player_id": 401, "drop_player_id": 113, "bid_amount": 9},
    ).json()

    assert body["submitted"] is False
    assert body["payload"]["bidAmount"] == 9.0
    assert write_requests(requests_log) == []


def test_a_real_claim_posts_the_add_and_the_drop(http, requests_log):
    body = http.post(
        "/api/waivers/claim",
        json={
            "add_player_id": 401,
            "drop_player_id": 113,
            "bid_amount": 9,
            "dry_run": False,
        },
    ).json()

    assert body["submitted"] is True

    import json
    sent = json.loads(write_requests(requests_log)[0].content)
    kinds = {item["type"] for item in sent["items"]}
    assert kinds == {"ADD", "DROP"}


def test_a_full_roster_claim_without_a_drop_is_rejected(http, requests_log):
    response = http.post(
        "/api/waivers/claim", json={"add_player_id": 401, "dry_run": False}
    )

    assert response.status_code == 400
    assert "drop" in response.json()["detail"].lower()
    assert write_requests(requests_log) == []


def test_read_only_mode_refuses_writes(http, requests_log):
    api._service.settings.read_only = True

    response = http.post("/api/lineup/apply", json={"dry_run": False})

    assert response.status_code == 403
    assert write_requests(requests_log) == []
    api._service.settings.read_only = False


def test_missing_cookies_refuse_writes(http, requests_log):
    api._service.settings.espn_s2 = ""

    response = http.post("/api/waivers/claim", json={"add_player_id": 401, "dry_run": False})

    assert response.status_code == 403
    assert write_requests(requests_log) == []
    api._service.settings.espn_s2 = "cookie-value"


def test_power_rankings_endpoint_ranks_every_team(http):
    body = http.get("/api/scouting/power-rankings").json()

    assert len(body["rankings"]) == 3
    assert body["rankings"][0]["power_rank"] == 1


def test_opponent_endpoint_returns_the_scouting_report(http):
    body = http.get("/api/scouting/opponent").json()

    assert body["has_opponent"] is True
    assert body["opponent"]["team_id"] == 2
    assert body["positional_edges"]


def test_trade_targets_endpoint_returns_partners(http):
    body = http.get("/api/scouting/trade-targets").json()

    assert body["targets"]
    assert body["targets"][0]["players"]


def test_positional_surplus_endpoint_covers_the_league(http):
    body = http.get("/api/scouting/positional-surplus").json()
    assert len(body["rows"]) == 3


def test_pending_transactions_are_exposed(http):
    body = http.get("/api/transactions/pending").json()
    assert body["pending"][0]["type"] == "WAIVER"


def test_the_ui_is_served_at_the_root(http):
    response = http.get("/")

    assert response.status_code == 200
    assert "Fantasy Command Center" in response.text


def test_an_unconfigured_league_explains_what_is_missing(monkeypatch):
    api.reset_service()
    monkeypatch.setattr(api, "load_settings", lambda: __import__(
        "ffopt.config", fromlist=["Settings"]).Settings())

    with TestClient(api.app) as test_client:
        response = test_client.get("/api/league")

    assert response.status_code == 400
    assert "ESPN_LEAGUE_ID" in str(response.json()["detail"]["missing"])
    api.reset_service()
