"""The prediction record: snapshots, settling results, and calibration."""

from conftest import ELIGIBLE

from ffopt import constants as C
from ffopt import history
from ffopt.models import League, LeagueSettings, Matchup, Player, Team


def starter(player_id, position, points, slot):
    return Player(
        player_id=player_id,
        name=f"{position}{player_id}",
        position=position,
        pro_team="KC",
        eligible_slots=ELIGIBLE[position],
        lineup_slot=slot,
        projected_points=points,
    )


def league_with(matchups, week, home_points=110.0, away_points=100.0):
    # One starter per team keeps the numbers easy to reason about.
    home = Team(team_id=1, name="Home", roster=[starter(1, "RB", home_points, 2)])
    away = Team(team_id=2, name="Away", roster=[starter(2, "RB", away_points, 2)])
    return League(
        settings=LeagueSettings(),
        teams=[home, away],
        matchups=matchups,
        week=week,
    )


def matchup(week, home_score=0.0, away_score=0.0):
    return Matchup(
        week=week,
        home_team_id=1,
        away_team_id=2,
        home_score=home_score,
        away_score=away_score,
    )


def test_a_prediction_is_recorded_for_the_current_week_only():
    conn = history.open_db(":memory:")
    league = league_with([matchup(3), matchup(4)], week=3)
    assert history.record_predictions(conn, league, 2025) == 1
    weeks = [r["week"] for r in conn.execute("SELECT week FROM predictions")]
    assert weeks == [3]


def test_favorite_gets_more_than_half():
    conn = history.open_db(":memory:")
    history.record_predictions(conn, league_with([matchup(3)], 3), 2025)
    row = conn.execute("SELECT p_home FROM predictions").fetchone()
    assert row["p_home"] > 0.5


def test_snapshotting_again_updates_but_a_finished_game_is_frozen():
    conn = history.open_db(":memory:")
    history.record_predictions(conn, league_with([matchup(3)], 3), 2025, now=1.0)
    history.record_predictions(
        conn, league_with([matchup(3)], 3, home_points=150.0), 2025, now=2.0
    )
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1
    assert conn.execute("SELECT home_mean FROM predictions").fetchone()[0] == 150.0

    settled = history.record_results(conn, league_with([matchup(3, 120, 90)], 4), 2025)
    assert settled == 1

    history.record_predictions(
        conn, league_with([matchup(3)], 3, home_points=10.0), 2025, now=3.0
    )
    assert conn.execute("SELECT home_mean FROM predictions").fetchone()[0] == 150.0


def test_results_are_not_recorded_for_the_week_in_progress():
    conn = history.open_db(":memory:")
    league = league_with([matchup(3, 50, 40)], week=3)
    history.record_predictions(conn, league, 2025)
    assert history.record_results(conn, league, 2025) == 0


def test_calibration_counts_each_game_from_both_sides():
    conn = history.open_db(":memory:")
    history.record_predictions(conn, league_with([matchup(3)], 3), 2025)
    history.record_results(conn, league_with([matchup(3, 120, 90)], 4), 2025)

    report = history.calibration(conn)
    assert report["games"] == 1
    assert sum(b["count"] for b in report["bins"]) == 2
    # The favorite won, so the model should score better than a coin flip.
    assert report["brier"] < 0.25


def test_calibration_with_no_games_is_empty():
    conn = history.open_db(":memory:")
    history.record_predictions(conn, league_with([matchup(3)], 3), 2025)
    assert history.calibration(conn) == {"games": 0, "bins": []}
    assert history.pending_count(conn) == 1


def test_a_tie_counts_as_half_a_win_each():
    conn = history.open_db(":memory:")
    history.record_predictions(conn, league_with([matchup(3)], 3), 2025)
    history.record_results(conn, league_with([matchup(3, 100, 100)], 4), 2025)
    report = history.calibration(conn)
    assert all(b["actual"] == 0.5 for b in report["bins"])


def test_backfill_rebuilds_earlier_weeks():
    conn = history.open_db(":memory:")
    leagues = {w: league_with([matchup(w)], w) for w in (1, 2, 3)}
    assert history.backfill(conn, lambda w: leagues[w], 2025, current_week=3) == 2
    weeks = sorted(r["week"] for r in conn.execute("SELECT week FROM predictions"))
    assert weeks == [1, 2]
