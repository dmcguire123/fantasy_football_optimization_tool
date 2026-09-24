"""
Multi-source projections: scoring, source parsing, blending, and wiring.

Every source is served from a mock transport, so nothing touches the network.
"""

import json

import httpx
import pytest

from ffopt import winprob
from ffopt.config import Settings
from ffopt.intel import stash
from ffopt.models import Player, extract_ros_points, extract_week_points
from ffopt.optimizer import season_projection
from ffopt.projections import store
from ffopt.projections.consensus import blend
from ffopt.projections.ids import PlayerIds, clean_name
from ffopt.projections.scoring import (
    LeagueScoring,
    PASS_YDS,
    RECEPTIONS,
    REC_YDS,
    expected_blocks,
    to_espn_stats,
)
from ffopt.projections.service import ProjectionService, apply, projected_only
from ffopt.projections.sources import (
    parse_espn_weekly,
    parse_fantasypros,
    parse_sleeper,
)


SEASON = 2026
WEEK = 3

# A league with full PPR and a half-point TE premium, as ESPN writes it.
SCORING_SETTINGS = {
    "scoringItems": [
        {"statId": 3, "points": 0.04},
        {"statId": 4, "points": 4.0},
        {"statId": 20, "points": -2.0},
        {"statId": 24, "points": 0.1},
        {"statId": 25, "points": 6.0},
        {"statId": 42, "points": 0.1},
        {"statId": 43, "points": 6.0},
        {"statId": 53, "points": 1.0, "pointsOverrides": {"6": 1.5}},
        {"statId": 72, "points": -2.0},
    ]
}


# ------------------------------------------------------------ scoring


def test_league_scoring_reads_points_and_position_overrides():
    scoring = LeagueScoring.from_settings(SCORING_SETTINGS)
    assert scoring.points_for(RECEPTIONS, "WR") == 1.0
    assert scoring.points_for(RECEPTIONS, "TE") == 1.5
    assert scoring.points_for(PASS_YDS, "QB") == 0.04
    assert scoring.points_for(999, "QB") == 0.0


def test_score_named_stat_line():
    scoring = LeagueScoring.from_settings(SCORING_SETTINGS)
    line = {"rec": 5, "rec_yds": 60, "rec_td": 0.5, "fumbles_lost": 0.1}
    assert scoring.score_named(line, "WR") == pytest.approx(5 + 6 + 3 - 0.2)
    assert scoring.score_named(line, "TE") == pytest.approx(7.5 + 6 + 3 - 0.2)


def test_yard_blocks_are_derived_for_leagues_that_score_them():
    # One point per 25 passing yards instead of 0.04 per yard.
    scoring = LeagueScoring.from_settings({"scoringItems": [{"statId": 8, "points": 1.0}]})
    stats = to_espn_stats({"pass_yds": 250})
    assert stats[8] == pytest.approx(expected_blocks(250, 25))
    assert scoring.score(stats, "QB") == pytest.approx(10 - 24 / 50)


def test_incompletions_are_derived():
    stats = to_espn_stats({"pass_att": 30, "pass_cmp": 20})
    assert stats[2] == 10


# ------------------------------------------------------ ESPN stat lines


def test_week_points_ignore_last_seasons_line_for_the_same_week():
    entries = [
        {"seasonId": 2025, "scoringPeriodId": 4, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 19.0},
        {"seasonId": 2026, "scoringPeriodId": 4, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": 25.9},
    ]
    assert extract_week_points(entries, 4, 1, season=2026) == 25.9


def test_ros_points_sum_remaining_weeks_and_count_games():
    entries = [
        {"seasonId": 2026, "scoringPeriodId": w, "statSourceId": 1, "statSplitTypeId": 1, "appliedTotal": pts}
        for w, pts in [(2, 50.0), (3, 10.0), (4, 0.0), (5, 12.0)]
    ]
    assert extract_ros_points(entries, 2026, 3, 5) == (22.0, 2)
    # Only the current week present is not a rest-of-season projection.
    assert extract_ros_points(entries[:2], 2026, 3, 5) is None


# ------------------------------------------------------------ sources


def sleeper_rows():
    return [
        {
            "player_id": "9221",
            "team": "DET",
            "player": {"first_name": "Jahmyr", "last_name": "Gibbs", "position": "RB"},
            "stats": {"rush_yd": 80.0, "rush_td": 0.8, "rec": 4.0, "rec_yd": 30.0,
                      "fum_lost": 0.1, "pts_std": 15.6, "pts_half_ppr": 17.6, "pts_ppr": 19.6},
        },
        {
            "player_id": "1",
            "team": "WAS",
            "player": {"first_name": "Rookie", "last_name": "Receiver Jr.", "position": "WR"},
            "stats": {"rec": 3.0, "rec_yd": 40.0, "pts_std": 4.0, "pts_half_ppr": 5.5, "pts_ppr": 7.0},
        },
        {
            "player_id": "DEN",
            "team": "DEN",
            "player": {"first_name": "Denver", "last_name": "Broncos", "position": "DEF"},
            "stats": {"sack": 3.0, "pts_std": 7.5, "pts_half_ppr": 7.5, "pts_ppr": 7.5},
        },
        # Listed but not projected: skipped.
        {"player_id": "2", "player": {"position": "RB"}, "stats": {}},
    ]


def test_parse_sleeper_maps_stats_positions_and_teams():
    rows = parse_sleeper(sleeper_rows())
    assert [r.position for r in rows] == ["RB", "WR", "D/ST"]
    gibbs = rows[0]
    assert gibbs.stats["rush_yds"] == 80.0
    assert gibbs.stats["fumbles_lost"] == 0.1
    assert rows[1].team == "WSH"
    assert rows[2].points["std"] == 7.5


def fantasypros_payload(points=18.0):
    return {
        "players": [
            {
                "fpid": "22968",
                "name": "Jahmyr Gibbs",
                "position_id": "RB",
                "team_id": "DET",
                "stats": [{"points": points - 4, "points_half": points - 2, "points_ppr": points,
                           "rush_yds": 75.0, "rush_tds": 0.7, "rec_rec": 4.0, "rec_yds": 28.0,
                           "fumbles": 0.1, "2pt_tds": 0.05}],
            }
        ]
    }


def test_parse_fantasypros_reads_stat_list_and_two_point_tries():
    rows = parse_fantasypros(fantasypros_payload())
    assert rows[0].stats["rush_2pt"] == 0.05
    assert rows[0].stats["fumbles_lost"] == 0.1
    assert rows[0].points["ppr"] == 18.0


def espn_public_payload():
    def entry(season, week, stats):
        return {"seasonId": season, "scoringPeriodId": week, "statSourceId": 1,
                "statSplitTypeId": 1, "stats": {str(k): v for k, v in stats.items()}}

    return {
        "players": [
            {
                "player": {
                    "id": 4429795,
                    "defaultPositionId": 2,
                    "stats": [
                        entry(2025, 3, {24: 500.0}),
                        entry(2026, 2, {24: 500.0}),
                        entry(2026, 3, {24: 100.0, 53: 5.0}),
                        entry(2026, 4, {24: 50.0}),
                    ],
                }
            }
        ]
    }


def test_parse_espn_weekly_keeps_only_this_season():
    players = parse_espn_weekly(espn_public_payload(), 2026)
    position, weeks = players["4429795"]
    assert position == "RB"
    assert sorted(weeks) == [2, 3, 4]
    assert weeks[3] == {24: 100.0, 53: 5.0}


# ------------------------------------------------------------ blending


def test_blend_averages_and_reports_spread():
    mean, spread = blend({"espn": 10.0, "sleeper": 14.0})
    assert mean == 12.0
    assert spread == 2.0


def test_blend_trims_high_and_low_with_four_sources():
    mean, _ = blend({"a": 1.0, "b": 10.0, "c": 12.0, "d": 40.0})
    assert mean == 11.0


def test_blend_leaves_out_zero_weight_sources():
    mean, _ = blend({"espn": 10.0, "model": 30.0})
    assert mean == 10.0
    assert blend({}) == (0.0, 0.0)


def test_stale_zero_projection_is_left_out_unless_everyone_says_zero():
    assert projected_only({"espn": 0.0, "sleeper": 16.1}) == {"sleeper": 16.1}
    assert projected_only({"espn": 0.0, "sleeper": 0.0}) == {"espn": 0.0, "sleeper": 0.0}


def test_clean_name_drops_punctuation_and_suffixes():
    assert clean_name("D.K. Metcalf Jr.") == clean_name("DK Metcalf")
    assert clean_name("Amon-Ra St. Brown") == "amonrastbrown"


# ------------------------------------------------------- service wiring


def mock_http(fantasypros=True):
    def handler(request):
        url = str(request.url)
        if "sleeper" in url:
            week = int(request.url.path.rstrip("/").split("/")[-1])
            rows = sleeper_rows() if week == WEEK else sleeper_rows()[:1]
            return httpx.Response(200, json=rows)
        if "fantasypros" in url:
            if not fantasypros:
                return httpx.Response(403, json={})
            position = request.url.params.get("position")
            if request.headers.get("x-api-key") != "KEY" or position != "RB":
                return httpx.Response(200, json={"players": []})
            ros = request.url.params.get("ros") == "true"
            return httpx.Response(200, json=fantasypros_payload(200.0 if ros else 18.0))
        if "leaguedefaults" in url:
            assert json.loads(request.headers["X-Fantasy-Filter"])["players"]["limit"]
            return httpx.Response(200, json=espn_public_payload())
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def id_table():
    return PlayerIds(
        [{"espn_id": "4429795", "sleeper_id": "9221", "fantasypros_id": "22968", "gsis_id": "00-1"}]
    )


def make_service(tmp_path, **kwargs):
    settings = Settings(fantasypros_api_key="KEY", projections_ttl_seconds=60, **kwargs)
    return ProjectionService(
        settings, http=mock_http(), db_path=tmp_path / "p.db", ids=id_table()
    )


def build(service):
    scoring = LeagueScoring.from_settings(SCORING_SETTINGS)
    return service.build(SEASON, WEEK, 4, scoring)


def gibbs():
    return Player(player_id=4429795, name="Jahmyr Gibbs", position="RB", pro_team="DET",
                  projected_points=17.0, espn_projected_points=17.0)


def test_build_scores_sources_with_league_rules_and_archives(tmp_path):
    service = make_service(tmp_path)
    projection_set = build(service)

    weekly = projection_set.weekly[("espn", "4429795")]
    # Sleeper: 8 rush + 4.8 TD + 4 rec + 3 yds - 0.2 fumble, in this league.
    assert weekly["sleeper"] == pytest.approx(19.6)
    assert weekly["fantasypros"] == pytest.approx(7.5 + 4.2 + 4 + 2.8 - 0.2)

    connection = store.open_db(tmp_path / "p.db")
    sources = {row["source"] for row in store.latest_pull(connection, SEASON, WEEK)}
    assert sources == {"sleeper", "fantasypros", "fantasypros_ros"}


def test_build_is_cached(tmp_path):
    service = make_service(tmp_path)
    assert build(service) is build(service)


def test_apply_replaces_projection_with_the_blend(tmp_path):
    projection_set = build(make_service(tmp_path))
    player = gibbs()
    apply([player], projection_set)

    assert set(player.source_points) == {"espn", "sleeper", "fantasypros"}
    expected = (17.0 + 19.6 + 18.3) / 3
    assert player.consensus_points == pytest.approx(expected)
    assert player.projected_points == pytest.approx(expected)
    assert player.espn_projected_points == 17.0
    assert player.consensus_spread > 0


def test_apply_builds_rest_of_season_from_every_source(tmp_path):
    projection_set = build(make_service(tmp_path))
    player = gibbs()
    apply([player], projection_set)

    # ESPN weeks 3-4 rescored: 10 + 5 receptions, then 5. Sleeper weeks 3-4:
    # 19.6 twice. FantasyPros rest of season is a total.
    espn_ros = 15.0 + 5.0
    sleeper_ros = 19.6 * 2
    fp_ros = parse_fantasypros(fantasypros_payload(200.0))[0]
    fp_points = LeagueScoring.from_settings(SCORING_SETTINGS).score_named(fp_ros.stats, "RB")
    assert player.ros_points == pytest.approx((espn_ros + sleeper_ros + fp_points) / 3)
    assert player.ros_games == 2


def test_apply_matches_by_name_and_defenses_by_team(tmp_path):
    projection_set = build(make_service(tmp_path))
    rookie = Player(player_id=5, name="Rookie Receiver", position="WR", pro_team="WSH",
                    espn_projected_points=6.0, projected_points=6.0)
    defense = Player(player_id=-16007, name="Broncos D/ST", position="D/ST", pro_team="DEN",
                     espn_projected_points=5.5, projected_points=5.5)
    apply([rookie, defense], projection_set)

    assert rookie.source_points["sleeper"] == pytest.approx(7.0)
    assert defense.source_points["sleeper"] == pytest.approx(7.5)
    assert defense.projected_points == pytest.approx(6.5)


def test_apply_can_leave_espn_in_charge(tmp_path):
    projection_set = build(make_service(tmp_path))
    player = gibbs()
    apply([player], projection_set, use_consensus=False)
    assert player.projected_points == 17.0
    assert player.consensus_points > 17.0


def test_missing_fantasypros_key_still_blends_the_rest(tmp_path):
    settings = Settings(projections_ttl_seconds=0)
    service = ProjectionService(settings, http=mock_http(), db_path=tmp_path / "p.db", ids=id_table())
    player = gibbs()
    apply([player], build(service))
    assert set(player.source_points) == {"espn", "sleeper"}


def test_archive_espn_saves_one_row_per_player(tmp_path):
    service = make_service(tmp_path)
    assert service.archive_espn(SEASON, WEEK, [gibbs(), gibbs()]) == 1
    connection = store.open_db(tmp_path / "p.db")
    assert store.summary(connection, SEASON)[0]["source"] == "espn"


# ------------------------------------------------ downstream consumers


def test_season_projection_prefers_rest_of_season_total():
    player = Player(player_id=1, name="A", position="RB", pro_team="DET",
                    projected_points=10.0, season_projected_points=300.0,
                    ros_points=120.0, ros_games=10)
    assert season_projection(player) == 120.0
    assert stash.typical_week(player) == 12.0


def test_source_disagreement_widens_player_spread():
    steady = Player(player_id=1, name="A", position="RB", pro_team="DET",
                    projected_points=10.0, stddev_ratio=0.5)
    disputed = Player(player_id=2, name="B", position="RB", pro_team="DET",
                      projected_points=10.0, stddev_ratio=0.5, consensus_spread=3.0)
    assert winprob.player_stddev(steady) == pytest.approx(5.0)
    assert winprob.player_stddev(disputed) == pytest.approx(34 ** 0.5)
