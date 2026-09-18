"""nflverse spreads: parsing, blending, caching. No test touches the network."""

import os
import time

import pytest

from conftest import ELIGIBLE

from ffopt import constants as C
from ffopt.models import Player
from ffopt.nflverse import (
    SHRINKAGE_GAMES,
    SpreadModel,
    _fetch_cached,
    build_id_map,
    build_spreads,
)
from ffopt.winprob import POSITION_STDDEV_RATIO, player_stddev


def game(gsis_id, position, points, season_type="REG"):
    return {
        "player_id": gsis_id,
        "position": position,
        "season_type": season_type,
        "fantasy_points_ppr": str(points),
    }


def make(player_id, position, points):
    return Player(
        player_id=player_id,
        name="P",
        position=position,
        pro_team="KC",
        eligible_slots=ELIGIBLE[position],
        lineup_slot=C.BENCH_SLOT,
        projected_points=points,
    )


def test_a_steady_player_has_a_smaller_spread_than_a_streaky_one():
    rows = [game("steady", "WR", p) for p in (10, 11, 10, 11, 10, 11)]
    rows += [game("streaky", "WR", p) for p in (2, 25, 4, 22, 3, 24)]
    ratios, _ = build_spreads(rows)
    assert ratios["steady"][1] < ratios["streaky"][1]
    assert ratios["steady"][0] == 6


def test_playoff_games_and_thin_samples_are_ignored():
    rows = [game("a", "RB", p, season_type="POST") for p in (10, 12, 9, 15)]
    rows += [game("b", "RB", p) for p in (10, 12)]
    rows += [game("c", "RB", p) for p in (0.5, 1.0, 0.0, 1.5)]
    ratios, _ = build_spreads(rows)
    assert ratios == {}


def test_position_ratio_is_the_median_of_its_players():
    rows = []
    for i in range(5):
        rows += [game(f"w{i}", "WR", p) for p in (8, 12, 8, 12)]
    _, positions = build_spreads(rows)
    assert 0.1 < positions["WR"] < 0.4


def test_id_map_skips_missing_ids():
    rows = [
        {"espn_id": "1", "gsis_id": "00-1"},
        {"espn_id": "NA", "gsis_id": "00-2"},
        {"espn_id": "3", "gsis_id": ""},
    ]
    assert build_id_map(rows) == {"1": "00-1"}


def test_own_history_blends_toward_the_position_by_sample_size():
    model = SpreadModel(
        player_ratios={"g1": (SHRINKAGE_GAMES, 0.2), "g2": (60, 0.2)},
        position_ratios={"WR": 0.8},
        id_map={"1": "g1", "2": "g2"},
    )
    short = model.ratio_for(1, "WR")
    long = model.ratio_for(2, "WR")
    assert abs(short - 0.5) < 1e-9
    assert long < short
    assert model.ratio_for(99, "WR") == 0.8
    assert model.ratio_for(99, "D/ST") is None


def test_apply_changes_the_players_score_spread():
    model = SpreadModel({"g": (30, 0.3)}, {"WR": 0.8}, {"1": "g"})
    steady = make(1, "WR", 15.0)
    unknown = make(2, "D/ST", 8.0)
    model.apply([steady, unknown])
    assert player_stddev(steady) < 0.5 * 15.0
    assert unknown.stddev_ratio == 0.0
    assert player_stddev(unknown) == POSITION_STDDEV_RATIO["D/ST"] * 8.0


class FakeResponse:
    def __init__(self, text, fail=False):
        self.text = text
        self.fail = fail

    def raise_for_status(self):
        if self.fail:
            raise RuntimeError("boom")


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.response


def test_downloads_are_cached_and_a_stale_copy_survives_failure(tmp_path):
    path = tmp_path / "f.csv"
    client = FakeClient(FakeResponse("a,b"))
    assert _fetch_cached("u", path, 3600, client=client) == "a,b"
    assert _fetch_cached("u", path, 3600, client=client) == "a,b"
    assert client.calls == 1

    old = time.time() - 7200
    os.utime(path, (old, old))
    failing = FakeClient(FakeResponse("", fail=True))
    assert _fetch_cached("u", path, 3600, client=failing) == "a,b"
    assert failing.calls == 1


def test_a_failed_download_with_no_cache_raises(tmp_path):
    with pytest.raises(RuntimeError):
        _fetch_cached("u", tmp_path / "x.csv", 3600, client=FakeClient(FakeResponse("", True)))
