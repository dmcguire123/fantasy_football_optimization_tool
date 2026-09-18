"""Configuration parsing and the guards that depend on it."""

import datetime

from ffopt.config import (
    Settings,
    default_season,
    normalize_swid,
    parse_env_file,
    load_settings,
)


def test_swid_is_wrapped_in_braces_either_way():
    assert normalize_swid("ABC-123") == "{ABC-123}"
    assert normalize_swid("{ABC-123}") == "{ABC-123}"
    assert normalize_swid("  ABC-123  ") == "{ABC-123}"
    assert normalize_swid("") == ""


def test_season_rolls_over_in_august():
    assert default_season(datetime.date(2026, 2, 10)) == 2025
    assert default_season(datetime.date(2026, 7, 31)) == 2025
    assert default_season(datetime.date(2026, 8, 1)) == 2026
    assert default_season(datetime.date(2026, 12, 25)) == 2026


def test_env_file_parsing_handles_quotes_comments_and_export(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "ESPN_LEAGUE_ID=123\n"
        'ESPN_S2="quoted value"\n'
        "export ESPN_TEAM_ID=4\n"
        "\n"
        "NOT_A_PAIR\n"
    )

    values = parse_env_file(env_file)
    assert values["ESPN_LEAGUE_ID"] == "123"
    assert values["ESPN_S2"] == "quoted value"
    assert values["ESPN_TEAM_ID"] == "4"
    assert "NOT_A_PAIR" not in values


def test_settings_report_what_is_missing():
    settings = load_settings(env={})
    assert "ESPN_LEAGUE_ID" in settings.missing_fields()

    settings = load_settings(env={"ESPN_LEAGUE_ID": "1", "ESPN_SEASON": "2025"})
    assert settings.missing_fields() == []
    assert settings.is_configured
    assert not settings.has_credentials
    assert not settings.can_write


def test_write_permission_needs_cookies_team_and_write_mode():
    base = dict(league_id="1", season=2025, team_id=2, swid="{X}", espn_s2="y")

    assert Settings(**base).can_write

    assert not Settings(**{**base, "swid": ""}).can_write
    assert not Settings(**{**base, "team_id": 0}).can_write
    assert not Settings(**{**base, "read_only": True}).can_write


def test_public_dict_never_leaks_cookies():
    settings = Settings(league_id="1", season=2025, swid="{secret}", espn_s2="secret2")
    exposed = settings.public_dict()

    assert "{secret}" not in str(exposed)
    assert "secret2" not in str(exposed)
    assert exposed["has_credentials"] is True
