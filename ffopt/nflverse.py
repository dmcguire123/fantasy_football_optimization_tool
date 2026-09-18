"""
Real week-to-week score spreads, from nflverse game logs.

The win-probability objective needs to know how much each player's score
swings from week to week. Rather than guess by position, this reads what
players actually scored in past games and measures it.

Two public files are used, both plain CSV so nothing extra needs installing:

  - nflverse weekly player stats, for every player's game-by-game fantasy points
  - the DynastyProcess id table, to map an ESPN player id to an nflverse id

A player's spread is kept as a share of their average score (their standard
deviation over their mean), then applied to ESPN's projection. Working in
shares means it does not matter that nflverse scores standard PPR while your
league may score differently.

A short history is noisy, so each player's own share is blended toward the
typical share for their position, leaning on the position more the fewer
games they have played.

Downloads are cached on disk. Everything here fails soft: with no network and
no cache, callers just keep the placeholder spreads.
"""

import csv
import io
import logging
import statistics
import time
from pathlib import Path

import httpx


log = logging.getLogger(__name__)

STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv"
)
ID_MAP_URL = "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv"

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "nflverse"

# How long a cached file is trusted. Finished seasons never change.
CURRENT_SEASON_TTL = 12 * 3600
ID_MAP_TTL = 7 * 24 * 3600

# Need this many games, and this many average points, before a player's own
# spread is worth anything. Below that the position's spread is used.
MIN_GAMES = 3
MIN_MEAN_POINTS = 3.0

# A player's own record counts as this many extra games of position average.
SHRINKAGE_GAMES = 6

# Bounds on any spread share, to keep a freak sample from dominating.
MIN_RATIO = 0.2
MAX_RATIO = 1.5

# Positions nflverse tracks that match ours. D/ST is not in the player stats.
SCORING_POSITIONS = {"QB", "RB", "WR", "TE", "K"}


class SpreadModel:
    """Score spreads by player and by position, as a share of average score."""

    def __init__(self, player_ratios, position_ratios, id_map):
        # nflverse (gsis) id -> (games, ratio)
        self.player_ratios = player_ratios
        # position -> typical ratio
        self.position_ratios = position_ratios
        # ESPN id -> nflverse (gsis) id
        self.id_map = id_map

    # The spread share for one player, or None when nothing is known and the
    # caller should fall back to its own default.
    def ratio_for(self, espn_id, position):
        position_ratio = self.position_ratios.get(position)
        gsis_id = self.id_map.get(str(espn_id))
        record = self.player_ratios.get(gsis_id) if gsis_id else None

        if record is None:
            return position_ratio

        games, ratio = record
        if position_ratio is None:
            return ratio
        blended = (games * ratio + SHRINKAGE_GAMES * position_ratio) / (
            games + SHRINKAGE_GAMES
        )
        return min(MAX_RATIO, max(MIN_RATIO, blended))

    # Attach the measured spread to each player that has one.
    def apply(self, players):
        for player in players:
            ratio = self.ratio_for(player.player_id, player.position)
            if ratio is not None:
                player.stddev_ratio = ratio
        return players


# Read a CSV file's text into dictionaries.
def _rows(text):
    return csv.DictReader(io.StringIO(text))


# Turn game logs into each player's (games, spread share) and each position's
# typical share. Only regular season games count.
def build_spreads(stat_rows):
    scores = {}
    positions = {}
    for row in stat_rows:
        if row.get("season_type") not in (None, "", "REG"):
            continue
        gsis_id = row.get("player_id")
        raw = row.get("fantasy_points_ppr")
        if not gsis_id or raw in (None, "", "NA"):
            continue
        try:
            points = float(raw)
        except ValueError:
            continue
        scores.setdefault(gsis_id, []).append(points)
        positions[gsis_id] = row.get("position") or ""

    player_ratios = {}
    by_position = {}
    for gsis_id, games in scores.items():
        if len(games) < MIN_GAMES:
            continue
        mean = statistics.fmean(games)
        if mean < MIN_MEAN_POINTS:
            continue
        ratio = min(MAX_RATIO, max(MIN_RATIO, statistics.stdev(games) / mean))
        player_ratios[gsis_id] = (len(games), ratio)
        by_position.setdefault(positions[gsis_id], []).append(ratio)

    position_ratios = {
        position: statistics.median(ratios)
        for position, ratios in by_position.items()
        if position in SCORING_POSITIONS and len(ratios) >= 5
    }
    return player_ratios, position_ratios


# Map ESPN player ids to nflverse ids from the id table.
def build_id_map(id_rows):
    mapping = {}
    for row in id_rows:
        espn_id = (row.get("espn_id") or "").strip()
        gsis_id = (row.get("gsis_id") or "").strip()
        if espn_id and gsis_id and espn_id != "NA" and gsis_id != "NA":
            mapping[espn_id] = gsis_id
    return mapping


# Fetch a file, using the on-disk copy while it is fresh and falling back to a
# stale copy if the download fails.
def _fetch_cached(url, cache_path, ttl, client=None, timeout=60.0):
    cache_path = Path(cache_path)
    fresh = (
        cache_path.exists()
        and (ttl is None or (time.time() - cache_path.stat().st_mtime) < ttl)
    )
    if fresh:
        return cache_path.read_text()

    try:
        http = client or httpx
        response = http.get(url, follow_redirects=True, timeout=timeout)
        response.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(response.text)
        return response.text
    except Exception as error:
        if cache_path.exists():
            log.warning("nflverse download failed (%s); using stale %s", error, cache_path)
            return cache_path.read_text()
        raise


# Build the spread model for a season, using it and the one before so early
# in the year there is still a sample to learn from.
def load_spread_model(season, cache_dir=DEFAULT_CACHE_DIR, client=None, seasons_back=1):
    cache_dir = Path(cache_dir)
    stat_rows = []
    for year in range(season - seasons_back, season + 1):
        ttl = CURRENT_SEASON_TTL if year >= season else None
        try:
            text = _fetch_cached(
                STATS_URL.format(season=year),
                cache_dir / f"stats_player_week_{year}.csv",
                ttl,
                client=client,
            )
        except Exception as error:
            # The current season may not exist yet in the offseason.
            log.info("no nflverse stats for %s (%s)", year, error)
            continue
        stat_rows.extend(_rows(text))

    if not stat_rows:
        raise RuntimeError("no nflverse stats available")

    player_ratios, position_ratios = build_spreads(stat_rows)
    id_text = _fetch_cached(
        ID_MAP_URL, cache_dir / "db_playerids.csv", ID_MAP_TTL, client=client
    )
    return SpreadModel(player_ratios, position_ratios, build_id_map(_rows(id_text)))
