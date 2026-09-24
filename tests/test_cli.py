"""The command line interface, with ESPN mocked and input stubbed."""

import pytest
from conftest import make_transport

from ffopt import cli
from ffopt.espn_client import EspnClient
from ffopt.league import LeagueService


@pytest.fixture
def wired(monkeypatch, settings, requests_log):
    espn = EspnClient(settings, transport=make_transport(requests_log))
    service = LeagueService(settings, client=espn)
    monkeypatch.setattr(cli, "build_service", lambda args: service)
    return service


def write_requests(log):
    return [r for r in log if r.method == "POST"]


def test_info_prints_the_league_and_team(wired, capsys):
    assert cli.main(["info"]) == 0
    output = capsys.readouterr().out

    assert "Test Gridiron League" in output
    assert "Week: 5" in output
    assert "My Squad" in output
    assert "FAAB" in output


def test_roster_lists_starters_and_bench(wired, capsys):
    assert cli.main(["roster"]) == 0
    output = capsys.readouterr().out

    assert "Bench Burner" in output
    assert "Left on bench" in output


def test_roster_can_target_another_team(wired, capsys):
    assert cli.main(["roster", "--team-id", "2"]) == 0
    assert "Rival Crew" in capsys.readouterr().out


def test_lineup_shows_the_moves_needed(wired, capsys):
    assert cli.main(["lineup"]) == 0
    output = capsys.readouterr().out

    assert "Optimal lineup" in output
    assert "Moves needed" in output
    assert "->" in output


def test_teams_prints_power_rankings(wired, capsys):
    assert cli.main(["teams"]) == 0
    output = capsys.readouterr().out

    assert "Power rankings" in output
    assert "Rival Crew" in output


def test_scout_describes_the_matchup(wired, capsys):
    assert cli.main(["scout"]) == 0
    output = capsys.readouterr().out

    assert "Rival Crew" in output
    assert "Win probability" in output
    assert "Position by position" in output


def test_waivers_lists_targets_with_ids(wired, capsys):
    assert cli.main(["waivers", "--limit", "5"]) == 0
    output = capsys.readouterr().out

    assert "Waiver targets" in output
    assert "401" in output


def test_trades_lists_partners_and_offers(wired, capsys):
    assert cli.main(["trades"]) == 0
    output = capsys.readouterr().out

    assert "Trade targets" in output
    assert "Ask for" in output


def test_declining_the_prompt_submits_nothing(wired, monkeypatch, capsys, requests_log):
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    assert cli.main(["apply-lineup"]) == 0
    assert "Cancelled" in capsys.readouterr().out
    assert write_requests(requests_log) == []


def test_confirming_the_prompt_submits_the_lineup(wired, monkeypatch, requests_log):
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    assert cli.main(["apply-lineup"]) == 0
    assert len(write_requests(requests_log)) == 1


def test_the_yes_flag_skips_the_prompt(wired, requests_log):
    assert cli.main(["apply-lineup", "--yes"]) == 0
    assert len(write_requests(requests_log)) == 1


def test_read_only_mode_refuses_to_apply(wired, requests_log):
    wired.settings.read_only = True
    try:
        assert cli.main(["apply-lineup", "--yes"]) == 2
        assert write_requests(requests_log) == []
    finally:
        wired.settings.read_only = False


def test_a_claim_needs_confirmation(wired, monkeypatch, requests_log):
    monkeypatch.setattr("builtins.input", lambda *a: "n")

    assert cli.main(["claim", "--add", "401", "--drop", "113", "--bid", "8"]) == 0
    assert write_requests(requests_log) == []


def test_a_confirmed_claim_is_submitted(wired, requests_log):
    exit_code = cli.main(
        ["claim", "--add", "401", "--drop", "113", "--bid", "8", "--yes"]
    )

    assert exit_code == 0
    import json
    sent = json.loads(write_requests(requests_log)[0].content)
    assert sent["type"] == "WAIVER"
    assert sent["bidAmount"] == 8.0


def test_a_free_agent_add_uses_the_other_transaction_type(wired, requests_log):
    cli.main(["claim", "--add", "401", "--free-agent", "--yes"])

    import json
    sent = json.loads(write_requests(requests_log)[0].content)
    assert sent["type"] == "FREEAGENT"


def test_espn_failures_are_reported_not_raised(wired, monkeypatch, capsys):
    from ffopt.espn_client import EspnError

    def boom(*args, **kwargs):
        raise EspnError("cookies expired")

    monkeypatch.setattr(wired, "load_league", boom)

    assert cli.main(["info"]) == 1
    assert "cookies expired" in capsys.readouterr().out


def test_the_parser_rejects_an_unknown_command():
    with pytest.raises(SystemExit):
        cli.main(["not-a-command"])


def test_report_prints_one_json_document_and_changes_nothing(wired, capsys, requests_log):
    import json

    assert cli.main(["report"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["week"] == 5
    assert report["team"] == "My Squad"
    assert {"lineup", "matchup", "roster_alerts", "waivers", "value"} <= set(report)
    # The misconfigured test roster starts a player on bye; the report says so.
    assert any(a["on_bye"] and a["starting"] for a in report["roster_alerts"])
    assert report["lineup"]["moves"]
    assert not write_requests(requests_log)
