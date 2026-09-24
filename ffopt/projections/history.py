"""
Historical data for building and grading projections.

Everything the backtest needs, loaded once and cached under
data/nflverse/history/ as parquet. Finished seasons never change, so their
files are kept for good; the current season is refetched after a day.

  - nflverse weekly player stats (2012 on): what players actually did. These
    are scored with the league's own rules to get actual fantasy points.
  - nflverse expected fantasy points (ffopportunity): how many points each
    player's opportunities were worth to an average player.
  - nflverse schedules: Vegas spread and total for every game.
  - nflverse injury reports: each week's official game status and practice
    participation, published before kickoff.
  - nflverse weekly FantasyPros ECR archive (2020 on): expert consensus ranks.
  - ESPN's weekly projections (2018 on), from its public feed.
  - Sleeper's weekly projections (Rotowire, 2018 on).

Every source is keyed by the nflverse (gsis) player id, using the
DynastyProcess id table to translate ESPN and Sleeper ids.
"""

import json
import logging
import time
from pathlib import Path

import httpx
import polars as pl

from ..nflverse import DEFAULT_CACHE_DIR
from .ids import PlayerIds
from .scoring import (
    PASS_YARD_BLOCKS,
    REC_YARD_BLOCKS,
    RUSH_YARD_BLOCKS,
    STAT_IDS,
    STAT_SCORED_POSITIONS,
)
from .sources import SLEEPER_URL, fetch_espn_weekly, parse_sleeper


log = logging.getLogger(__name__)

HISTORY_DIR = DEFAULT_CACHE_DIR / "history"
CURRENT_SEASON_TTL = 24 * 3600

FIRST_STATS_SEASON = 2012

# nflverse stat columns for each of our plain stat names. Fumbles lost and
# two-point tries are spread over several columns and are added up.
NFLVERSE_STATS = {
    "pass_att": ["attempts"],
    "pass_cmp": ["completions"],
    "pass_yds": ["passing_yards"],
    "pass_td": ["passing_tds"],
    "pass_int": ["passing_interceptions"],
    "pass_2pt": ["passing_2pt_conversions"],
    "rush_att": ["carries"],
    "rush_yds": ["rushing_yards"],
    "rush_td": ["rushing_tds"],
    "rush_2pt": ["rushing_2pt_conversions"],
    "rec": ["receptions"],
    "rec_yds": ["receiving_yards"],
    "rec_td": ["receiving_tds"],
    "rec_2pt": ["receiving_2pt_conversions"],
    "targets": ["targets"],
    "fumbles_lost": ["rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"],
}


# Load a parquet file from the cache, or build it and save it. Seasons that
# are over are kept forever; the current one is rebuilt after a day.
def _cached_frame(path, build, current=False):
    path = Path(path)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if not current or age < CURRENT_SEASON_TTL:
            return pl.read_parquet(path)
    frame = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return frame


def _is_current(season):
    from ..config import default_season

    return season >= default_season()


# ------------------------------------------------------------- nflverse


def load_player_stats(seasons):
    import nflreadpy as nfl

    frames = []
    for season in seasons:
        frames.append(
            _cached_frame(
                HISTORY_DIR / f"player_stats_{season}.parquet",
                lambda season=season: nfl.load_player_stats(season),
                current=_is_current(season),
            )
        )
    stats = pl.concat(frames, how="diagonal_relaxed")
    return stats.filter(
        (pl.col("season_type") == "REG") & pl.col("position").is_in(list(STAT_SCORED_POSITIONS))
    )


def load_opportunity(seasons):
    import nflreadpy as nfl

    frames = []
    for season in seasons:
        try:
            frames.append(
                _cached_frame(
                    HISTORY_DIR / f"ff_opportunity_{season}.parquet",
                    lambda season=season: nfl.load_ff_opportunity(season),
                    current=_is_current(season),
                )
            )
        except Exception as error:
            log.info("no expected points for %s (%s)", season, error)
    return pl.concat(frames, how="diagonal_relaxed")


def load_injuries(seasons):
    import nflreadpy as nfl

    frames = []
    for season in seasons:
        try:
            frames.append(
                _cached_frame(
                    HISTORY_DIR / f"injuries_{season}.parquet",
                    lambda season=season: nfl.load_injuries(season),
                    current=_is_current(season),
                )
            )
        except Exception as error:
            log.info("no injury reports for %s (%s)", season, error)
    return pl.concat(frames, how="diagonal_relaxed")


def load_schedules():
    import nflreadpy as nfl

    return _cached_frame(HISTORY_DIR / "schedules.parquet", nfl.load_schedules, current=True)


# FantasyPros weekly expert consensus ranks, one row per player per week.
# The archive is stamped with the date it was taken, so each snapshot is
# assigned to the first week whose games had not all finished by then.
def load_weekly_ecr(schedules):
    import nflreadpy as nfl

    ranks = _cached_frame(
        HISTORY_DIR / "ff_rankings_all.parquet",
        lambda: nfl.load_ff_rankings("all"),
        current=True,
    )
    ranks = ranks.filter(
        pl.col("page_type").is_in(["weekly-qb", "weekly-rb", "weekly-wr", "weekly-te"])
    ).with_columns(pl.col("scrape_date").str.to_date())

    week_ends = (
        schedules.filter(pl.col("game_type") == "REG")
        .with_columns(pl.col("gameday").str.to_date())
        .group_by(["season", "week"])
        .agg(pl.col("gameday").min().alias("first_game"), pl.col("gameday").max().alias("last_game"))
        .sort("last_game")
    )
    ranks = ranks.sort("scrape_date").join_asof(
        week_ends, left_on="scrape_date", right_on="last_game", strategy="forward"
    )
    # A snapshot taken after that week's games began is too late to count.
    ranks = ranks.filter(pl.col("scrape_date") <= pl.col("first_game"))
    # Keep the last snapshot before each week.
    ranks = ranks.filter(
        pl.col("scrape_date") == pl.col("scrape_date").max().over(["season", "week"])
    )
    return ranks.select(
        pl.col("season"),
        pl.col("week"),
        pl.col("id").cast(pl.Utf8).alias("fantasypros_id"),
        pl.col("pos").alias("position"),
        pl.col("ecr").cast(pl.Float64),
    )


# ------------------------------------------------ scoring whole frames


# A polars expression for this league's fantasy points from nflverse stat
# columns. Per-N-yard stats use whole blocks, since these are real games.
def points_expression(scoring, position_column="position"):
    def stat(name):
        columns = NFLVERSE_STATS[name]
        total = pl.lit(0.0)
        for column in columns:
            total = total + pl.col(column).cast(pl.Float64).fill_null(0.0)
        return total

    def for_position(position):
        total = pl.lit(0.0)
        for name, stat_id in STAT_IDS.items():
            points = scoring.points_for(stat_id, position)
            if points:
                total = total + stat(name) * points
        for yards_name, blocks in (
            ("pass_yds", PASS_YARD_BLOCKS),
            ("rush_yds", RUSH_YARD_BLOCKS),
            ("rec_yds", REC_YARD_BLOCKS),
        ):
            for block_id, block in blocks:
                points = scoring.points_for(block_id, position)
                if points:
                    total = total + (stat(yards_name) / block).floor().clip(0) * points
        return total

    expression = pl.lit(None, dtype=pl.Float64)
    for position in sorted(STAT_SCORED_POSITIONS):
        expression = (
            pl.when(pl.col(position_column) == position)
            .then(for_position(position))
            .otherwise(expression)
        )
    return expression


# ------------------------------------------ ESPN and Sleeper projections


# A (source id, gsis id) table for one id column of the id map.
def _id_frame(ids, column):
    rows = []
    for espn_id, other_ids in ids.from_espn.items():
        source_id = espn_id if column == "espn_id" else other_ids.get(column)
        gsis_id = other_ids.get("gsis_id")
        if source_id and gsis_id:
            rows.append((source_id, gsis_id))
    return pl.DataFrame(rows, schema=["source_id", "player_id"], orient="row").unique("source_id")


# ESPN's weekly projected stat lines for one season, as rows of
# (espn id, position, week, stats as JSON). Scored later, per league.
def load_espn_projections(season, http=None):
    def build():
        players = fetch_espn_weekly(season, limit=1500, http=http)
        if not players:
            raise RuntimeError(f"ESPN projections unavailable for {season}")
        rows = []
        for espn_id, (position, weeks) in players.items():
            for week, stats in weeks.items():
                if week and position in STAT_SCORED_POSITIONS:
                    rows.append((espn_id, position, int(week), json.dumps(stats)))
        return pl.DataFrame(
            rows, schema=["source_id", "position", "week", "stats"], orient="row"
        ).with_columns(pl.lit(season).alias("season"))

    return _cached_frame(
        HISTORY_DIR / f"espn_projections_{season}.parquet", build, current=_is_current(season)
    )


# Sleeper's weekly projected stat lines for one season, same shape.
def load_sleeper_projections(season, weeks=range(1, 19), http=None):
    def build():
        client = http or httpx
        params = [("season_type", "regular")] + [
            ("position[]", p) for p in ("QB", "RB", "WR", "TE")
        ]
        rows = []
        for week in weeks:
            try:
                response = client.get(
                    SLEEPER_URL.format(season=season, week=week), params=params, timeout=60.0
                )
                response.raise_for_status()
            except Exception as error:
                log.info("no Sleeper projections for %s week %s (%s)", season, week, error)
                continue
            for row in parse_sleeper(response.json()):
                rows.append((row.source_id, row.position, week, json.dumps(row.stats)))
        if not rows:
            raise RuntimeError(f"Sleeper projections unavailable for {season}")
        return pl.DataFrame(
            rows, schema=["source_id", "position", "week", "stats"], orient="row"
        ).with_columns(pl.lit(season).alias("season"))

    return _cached_frame(
        HISTORY_DIR / f"sleeper_projections_{season}.parquet", build, current=_is_current(season)
    )


# Score stored projection rows with the league's rules and attach gsis ids.
# ESPN rows are in ESPN stat ids; Sleeper rows are in our plain names.
def score_projections(frame, scoring, ids, id_column, source):
    points = []
    for position, stats_json in zip(frame["position"], frame["stats"]):
        stats = json.loads(stats_json)
        if id_column == "espn_id":
            points.append(scoring.score({int(k): v for k, v in stats.items()}, position))
        else:
            points.append(scoring.score_named(stats, position))
    scored = frame.with_columns(pl.Series(source, points, dtype=pl.Float64)).join(
        _id_frame(ids, id_column), on="source_id", how="inner"
    )
    return scored.select("player_id", "season", "week", source).unique(
        ["player_id", "season", "week"]
    )


def load_player_ids():
    return PlayerIds.load()

