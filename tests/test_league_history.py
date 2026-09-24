"""The league history payload behind the dashboard."""

import json

from ffopt.league_history import load_history, load_season


def test_the_saved_history_joins_up():
    data = load_history()

    assert data["years"] == [2020, 2021, 2022, 2023, 2024, 2025]
    assert sum(o["titles"] for o in data["owners"]) == len(data["champions"]) == 6
    # 2020 had ten teams; every later year had twelve.
    sizes = {s["season"]: s["num_teams"] for s in data["seasons"]}
    assert sizes[2020] == 10 and sizes[2025] == 12
    assert sum(len(s["trades"]) for s in data["seasons"]) == 39


def test_every_row_has_an_owner_and_a_place():
    data = load_history()
    for season in data["seasons"]:
        for row in season["rows"]:
            assert row["owner_name"] != "Unknown"
            assert row["final_place"] is not None
            assert row["games"] == row["wins"] + row["losses"] + row["ties"]


def test_the_champion_row_matches_the_champion_list():
    data = load_history()
    for champ in data["champions"]:
        season = next(s for s in data["seasons"] if s["season"] == champ["season"])
        winner = next(r for r in season["rows"] if r["final_place"] == 1)
        assert winner["owner_name"] == champ["owner_name"]


def test_a_season_with_missing_optional_files_still_loads(tmp_path):
    folder = tmp_path / "2019"
    folder.mkdir()
    (folder / "rankings.json").write_text(json.dumps([
        {"team_id": 1, "name": "A", "rank": 1, "wins": 3, "losses": 1, "ties": 0,
         "points_for": 400.0, "points_against": 350.0},
    ]))
    season = load_season(tmp_path, 2019)
    assert season["rows"][0]["trades"] == 0
    assert season["rows"][0]["owner_name"] == "Unknown"
    assert season["trades"] == [] and season["draft"] is None


def test_no_history_folder_gives_none(tmp_path):
    assert load_history(tmp_path / "nothing") is None
