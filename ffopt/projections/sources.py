"""
Fetch weekly projections from outside sources.

Each source returns a list of SourceProjection records in the same shape:
the source's own player id, a stat line in our plain stat names (see
scoring.STAT_IDS), and the source's own points in the generic formats
(standard, half PPR, full PPR). The service scores the stat line with the
league's rules, and falls back to the generic points when it has to.

  - Sleeper: public and keyless. Its projections come from Rotowire, and
    past weeks stay available, which makes it useful for backtesting.
  - FantasyPros: official API, needs a free personal key in
    FANTASYPROS_API_KEY. Their projections are an average over experts.
  - ESPN, public feed: ESPN's weekly stat-line projections for every week of
    the season. The league feed only carries the current week, so this is
    where ESPN's rest-of-season numbers come from. The stat lines are already
    in ESPN stat ids, so they are scored directly with the league's rules.

Everything fails soft: a source that cannot be reached returns no rows, and
the consensus is built from whatever did come back.
"""

import json
import logging
from dataclasses import dataclass, field

import httpx

from .. import constants as C


log = logging.getLogger(__name__)

SLEEPER_URL = "https://api.sleeper.app/projections/nfl/{season}/{week}"
FANTASYPROS_URL = "https://api.fantasypros.com/public/v2/json/nfl/{season}/projections"
ESPN_PUBLIC_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}"
    "/segments/0/leaguedefaults/3"
)

POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")

# Sleeper and FantasyPros name some teams differently from ESPN.
TEAM_ALIASES = {"WAS": "WSH", "JAC": "JAX", "LA": "LAR", "OAK": "LV", "SD": "LAC"}


@dataclass
class SourceProjection:
    """One player's projection for one week, from one source."""

    source: str
    source_id: str
    name: str
    position: str
    team: str
    stats: dict = field(default_factory=dict)
    # Generic-format points as the source scored them: "std", "half", "ppr".
    points: dict = field(default_factory=dict)


def espn_team(abbrev):
    abbrev = (abbrev or "").upper()
    return TEAM_ALIASES.get(abbrev, abbrev)


def _get_json(http, url, params=None, headers=None, timeout=30.0):
    response = http.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------- Sleeper

# Sleeper stat names, and the plain name each one maps to.
SLEEPER_STATS = {
    "pass_att": "pass_att",
    "pass_cmp": "pass_cmp",
    "pass_yd": "pass_yds",
    "pass_td": "pass_td",
    "pass_int": "pass_int",
    "pass_2pt": "pass_2pt",
    "rush_att": "rush_att",
    "rush_yd": "rush_yds",
    "rush_td": "rush_td",
    "rush_2pt": "rush_2pt",
    "rec": "rec",
    "rec_yd": "rec_yds",
    "rec_td": "rec_td",
    "rec_2pt": "rec_2pt",
    "rec_tgt": "targets",
    "fum": "fumbles",
    "fum_lost": "fumbles_lost",
}

SLEEPER_POSITIONS = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DEF": "D/ST"}


# Turn Sleeper's projection rows into SourceProjections. Sleeper lists a lot
# of players it does not actually project; those have no points and are
# skipped.
def parse_sleeper(rows):
    projections = []
    for row in rows or []:
        stats = row.get("stats") or {}
        if stats.get("pts_ppr") is None:
            continue
        player = row.get("player") or {}
        position = SLEEPER_POSITIONS.get(player.get("position"))
        if not position:
            continue

        name = f"{player.get('first_name') or ''} {player.get('last_name') or ''}".strip()
        named = {}
        for sleeper_name, plain_name in SLEEPER_STATS.items():
            if stats.get(sleeper_name) is not None:
                named[plain_name] = float(stats[sleeper_name])

        projections.append(
            SourceProjection(
                source="sleeper",
                source_id=str(row.get("player_id") or ""),
                name=name,
                position=position,
                team=espn_team(row.get("team") or player.get("team")),
                stats=named,
                points={
                    "std": float(stats.get("pts_std") or 0.0),
                    "half": float(stats.get("pts_half_ppr") or 0.0),
                    "ppr": float(stats.get("pts_ppr") or 0.0),
                },
            )
        )
    return projections


def fetch_sleeper(season, week, http=None):
    http = http or httpx
    params = [("season_type", "regular")]
    for position in SLEEPER_POSITIONS:
        params.append(("position[]", position))
    try:
        rows = _get_json(http, SLEEPER_URL.format(season=season, week=week), params=params)
    except Exception as error:
        log.warning("Sleeper projections unavailable for %s week %s: %s", season, week, error)
        return []
    return parse_sleeper(rows)


# ----------------------------------------------------------- FantasyPros

# FantasyPros stat names, and the plain name each one maps to. Their
# "fumbles" column is fumbles lost. "2pt_tds" is handled per position below.
FANTASYPROS_STATS = {
    "pass_att": "pass_att",
    "pass_cmp": "pass_cmp",
    "pass_yds": "pass_yds",
    "pass_tds": "pass_td",
    "pass_ints": "pass_int",
    "rush_att": "rush_att",
    "rush_yds": "rush_yds",
    "rush_tds": "rush_td",
    "rec_rec": "rec",
    "rec_yds": "rec_yds",
    "rec_tds": "rec_td",
    "fumbles": "fumbles_lost",
}

FANTASYPROS_POSITIONS = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K", "DST": "D/ST"}

# Which kind of two-point conversion a position most likely scores.
TWO_POINT_STAT = {"QB": "pass_2pt", "RB": "rush_2pt", "WR": "rec_2pt", "TE": "rec_2pt"}


def parse_fantasypros(payload, source="fantasypros"):
    projections = []
    for player in (payload or {}).get("players") or []:
        position = FANTASYPROS_POSITIONS.get(player.get("position_id"))
        if not position:
            continue
        stats = player.get("stats") or {}
        if isinstance(stats, list):
            stats = stats[0] if stats else {}
        if stats.get("points") is None:
            continue

        named = {}
        for fp_name, plain_name in FANTASYPROS_STATS.items():
            if stats.get(fp_name) is not None:
                named[plain_name] = float(stats[fp_name])
        if stats.get("2pt_tds") and position in TWO_POINT_STAT:
            named[TWO_POINT_STAT[position]] = float(stats["2pt_tds"])

        projections.append(
            SourceProjection(
                source=source,
                source_id=str(player.get("fpid") or ""),
                name=player.get("name") or "",
                position=position,
                team=espn_team(player.get("team_id")),
                stats=named,
                points={
                    "std": float(stats.get("points") or 0.0),
                    "half": float(stats.get("points_half") or 0.0),
                    "ppr": float(stats.get("points_ppr") or 0.0),
                },
            )
        )
    return projections


# Weekly projections, or rest-of-season totals when ros is True.
def fetch_fantasypros(season, week, api_key, http=None, ros=False):
    if not api_key:
        return []
    http = http or httpx
    projections = []
    for position in FANTASYPROS_POSITIONS:
        params = {"position": position, "week": week}
        if ros:
            params["ros"] = "true"
        try:
            payload = _get_json(
                http,
                FANTASYPROS_URL.format(season=season),
                params=params,
                headers={"x-api-key": api_key},
            )
        except Exception as error:
            log.warning("FantasyPros %s projections unavailable: %s", position, error)
            continue
        projections.extend(
            parse_fantasypros(payload, source="fantasypros_ros" if ros else "fantasypros")
        )
    return projections


# ------------------------------------------------------- ESPN public feed

# ESPN's weekly projected stat lines, for the most-owned players. Returns
# {espn id: (position, {week: {stat id: value}})}. Only projections
# (source 1) for single weeks (split 1) of the requested season are kept.
def parse_espn_weekly(payload, season):
    players = {}
    for record in (payload or {}).get("players") or []:
        raw = record.get("player") or {}
        weeks = {}
        for entry in raw.get("stats") or []:
            if entry.get("seasonId") != season:
                continue
            if entry.get("statSourceId") != C.STAT_SOURCE_PROJECTED:
                continue
            if entry.get("statSplitTypeId") != C.STAT_SPLIT_WEEK:
                continue
            stats = {int(k): float(v) for k, v in (entry.get("stats") or {}).items()}
            weeks[entry.get("scoringPeriodId")] = stats
        if weeks:
            position = C.position_name(raw.get("defaultPositionId", 0))
            players[str(raw.get("id"))] = (position, weeks)
    return players


def fetch_espn_weekly(season, limit=1000, http=None):
    http = http or httpx
    player_filter = {
        "players": {
            "limit": limit,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            "filterStatsForSourceIds": {"value": [C.STAT_SOURCE_PROJECTED]},
            "filterStatsForSplitTypeIds": {"value": [C.STAT_SPLIT_WEEK]},
        }
    }
    try:
        payload = _get_json(
            http,
            ESPN_PUBLIC_URL.format(season=season),
            params={"view": "kona_player_info"},
            headers={"X-Fantasy-Filter": json.dumps(player_filter)},
            timeout=60.0,
        )
    except Exception as error:
        log.warning("ESPN weekly projections unavailable for %s: %s", season, error)
        return {}
    return parse_espn_weekly(payload, season)
