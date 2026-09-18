"""Splitting run-together player text from trade emails into real players."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_email_history as B


def names(text):
    return [p["name"] for p in B.parse_players(text)]


def test_single_player():
    players = B.parse_players("S. BarkleyRB - PHI")

    assert players == [{"name": "S. Barkley", "position": "RB", "nfl_team": "PHI"}]


def test_players_run_together_are_split_at_the_next_initial():
    players = B.parse_players("T. McLaurinWR - WASW. RobinsonWR - NYG")

    assert [p["name"] for p in players] == ["T. McLaurin", "W. Robinson"]
    assert [p["nfl_team"] for p in players] == ["WAS", "NYG"]


def test_two_letter_team_followed_by_an_initial():
    players = B.parse_players("C. SuttonWR - DENT. AtwellWR - LAN. HarrisRB - PIT")

    assert [p["nfl_team"] for p in players] == ["DEN", "LA", "PIT"]
    assert [p["name"] for p in players] == ["C. Sutton", "T. Atwell", "N. Harris"]


def test_names_with_periods_and_hyphens():
    assert names("A. St. BrownWR - DETT. EtienneRB - JAX") == ["A. St. Brown", "T. Etienne"]
    assert names("J. Smith-SchusterWR - NER. WhiteRB - TB") == ["J. Smith-Schuster", "R. White"]


def test_defense_and_kicker_positions():
    players = B.parse_players("K. ChiefsDEF - KCM. EvansWR - TB")

    assert players[0]["position"] == "DEF"
    assert players[0]["nfl_team"] == "KC"
    assert players[1]["name"] == "M. Evans"


def test_text_that_does_not_round_trip_returns_none():
    assert B.parse_players("not a player list") is None


def test_truncated_team_names_match_by_prefix():
    teams = [
        {"team_id": 1, "name": "Dublin Bay Buccaneers", "owner_name": "Jon"},
        {"team_id": 2, "name": "Burton is a  Bitch", "owner_name": "Sam"},
    ]

    assert B.find_team(teams, "Dublin Bay ...")["team_id"] == 1
    assert B.find_team(teams, "Burton is a ...")["team_id"] == 2
    assert B.find_team(teams, "Nobody Here") is None


def test_moves_show_who_gave_and_who_got():
    trade = {
        "date": "2024-11-15T17:06:42Z",
        "message_id": "m1",
        "side_a": {"team": "A", "gives": [{"name": "N. Chubb", "position": "RB", "nfl_team": "CLE"}]},
        "side_b": {"team": "B", "gives": [{"name": "T. Higgins", "position": "WR", "nfl_team": "CIN"}]},
    }

    moves = B.build_moves([trade])
    by_player = {m["player"]: m for m in moves}

    assert by_player["N. Chubb"]["from_team"] == "A"
    assert by_player["N. Chubb"]["to_team"] == "B"
    assert by_player["T. Higgins"]["from_team"] == "B"
    assert by_player["T. Higgins"]["to_team"] == "A"


def test_draft_recap_is_joined_to_the_actual_finish(tmp_path):
    year_dir = tmp_path / "2023"
    year_dir.mkdir()
    B.write_json(str(year_dir / "teams.json"), [{"team_id": 10, "name": "Put the Kittle On", "owner_name": "Dave"}])
    B.write_json(str(year_dir / "rankings.json"), [{"team_id": 10, "rank": 1, "record": "11-4-0"}])
    B.write_json(str(year_dir / "playoffs.json"), {"teams": [{"team_id": 10, "final_place": 4}]})

    draft = B.build_draft(str(tmp_path), {"season": 2023, "grade": "A"}, "Put the Kittle On")

    assert draft["grade"] == "A"
    assert draft["actual"] == {
        "team_id": 10,
        "regular_season_rank": 1,
        "record": "11-4-0",
        "final_place": 4,
    }


def test_draft_recap_without_api_data_is_kept_as_is(tmp_path):
    draft = B.build_draft(str(tmp_path), {"season": 2020, "grade": None}, "Put the Kittle On")

    assert draft == {"season": 2020, "grade": None}


def test_every_saved_trade_parses(tmp_path):
    source = B.read_json(os.path.join(B.HISTORY_DIR, "source", "gmail_trade_processed.json"))

    for trade in source["trades"]:
        for side in ("side_a", "side_b"):
            assert B.parse_players(trade[side]["gives"]) is not None, trade[side]["gives"]
