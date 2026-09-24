"""
Normalized objects built from ESPN's raw JSON.

ESPN nests player data several layers deep and repeats stat lines for every
week and every projection source. These classes flatten that into something
the optimizer, the waiver logic, and the UI can all use directly.
"""

from dataclasses import dataclass, field

from . import constants as C


# Pull the fantasy points for one week out of a player's stat list.
# statSourceId 1 is ESPN's projection, 0 is what the player actually scored.
# ESPN can return last season's lines too, under the same week numbers, so
# the season has to match when the entry says which season it is.
def extract_week_points(stat_entries, week, source_id, season=None):
    for entry in stat_entries or []:
        if entry.get("scoringPeriodId") != week:
            continue
        if entry.get("statSourceId") != source_id:
            continue
        if entry.get("statSplitTypeId") not in (None, C.STAT_SPLIT_WEEK):
            continue
        if season and entry.get("seasonId") not in (None, season):
            continue
        total = entry.get("appliedTotal")
        if total is not None:
            return float(total)
    return None


# Pull a full-season total out of a player's stat list. Used for
# rest-of-season style comparisons and as a tiebreaker.
def extract_season_points(stat_entries, season, source_id):
    for entry in stat_entries or []:
        if entry.get("statSplitTypeId") != C.STAT_SPLIT_SEASON:
            continue
        if entry.get("statSourceId") != source_id:
            continue
        if entry.get("seasonId") not in (None, season):
            continue
        total = entry.get("appliedTotal")
        if total is not None:
            return float(total)
    return None


# ESPN's rest-of-season projection: the sum of its weekly projections from
# this week through the last week. Returns (points, games), where games counts
# the weeks projected above zero, or None when ESPN sent no future weeks.
def extract_ros_points(stat_entries, season, from_week, to_week):
    total = 0.0
    games = 0
    weeks_seen = set()
    for entry in stat_entries or []:
        if entry.get("statSourceId") != C.STAT_SOURCE_PROJECTED:
            continue
        if entry.get("statSplitTypeId") != C.STAT_SPLIT_WEEK:
            continue
        if entry.get("seasonId") not in (None, season):
            continue
        week = entry.get("scoringPeriodId") or 0
        if week < from_week or week > to_week or week in weeks_seen:
            continue
        weeks_seen.add(week)
        points = float(entry.get("appliedTotal") or 0.0)
        total += points
        if points > 0:
            games += 1
    # Only the current week is not a rest-of-season projection.
    if len(weeks_seen) < 2 and to_week > from_week:
        return None
    return total, games


@dataclass
class Player:
    """One NFL player or defense, as far as this league is concerned."""

    player_id: int
    name: str
    position: str
    pro_team: str
    eligible_slots: list = field(default_factory=list)
    lineup_slot: int = C.BENCH_SLOT
    injury_status: str = "ACTIVE"
    percent_owned: float = 0.0
    percent_started: float = 0.0
    projected_points: float = 0.0
    actual_points: float = 0.0
    season_projected_points: float = 0.0
    season_actual_points: float = 0.0
    on_bye: bool = False
    availability: str = C.STATUS_ONTEAM
    fantasy_team_id: int = 0
    acquisition_type: str = ""
    # Week-to-week score spread as a share of projection, when measured from
    # game logs. Zero means unknown, and the position default is used.
    stddev_ratio: float = 0.0
    # Projections from every source, scored for this league, keyed by source
    # name ("espn", "sleeper", "fantasypros"). Filled by the projections
    # service; see ffopt/projections.
    source_points: dict = field(default_factory=dict)
    # The blend of those sources, and how far apart they are (one standard
    # deviation). Zero means no blend was made.
    consensus_points: float = 0.0
    consensus_spread: float = 0.0
    # ESPN's own weekly projection, kept when projected_points is replaced
    # by the consensus.
    espn_projected_points: float = 0.0
    # Points still to come this season, and the games they come from. Zero
    # games means unknown.
    ros_points: float = 0.0
    ros_games: int = 0
    # Each source's rest-of-season total, keyed like source_points.
    ros_source_points: dict = field(default_factory=dict)

    # A player on bye or ruled out contributes nothing this week.
    @property
    def is_playable(self):
        if self.on_bye:
            return False
        return self.injury_status not in C.UNAVAILABLE_INJURY_STATUSES

    # The projection the optimizer should actually use for this week.
    @property
    def effective_projection(self):
        if not self.is_playable:
            return 0.0
        return self.projected_points

    @property
    def is_starting(self):
        return self.lineup_slot not in C.NON_SCORING_SLOTS

    @property
    def lineup_slot_name(self):
        return C.slot_name(self.lineup_slot)

    # Slots this player could actually start in, ignoring bench and IR.
    def startable_slots(self):
        slots = self.eligible_slots
        if not slots:
            slots = C.FALLBACK_ELIGIBLE_SLOTS.get(self.position, [])
        return [s for s in slots if s not in C.NON_SCORING_SLOTS]

    def to_dict(self):
        return {
            "player_id": self.player_id,
            "name": self.name,
            "position": self.position,
            "pro_team": self.pro_team,
            "lineup_slot": self.lineup_slot,
            "lineup_slot_name": self.lineup_slot_name,
            "eligible_slots": list(self.eligible_slots),
            "startable_slots": self.startable_slots(),
            "injury_status": C.INJURY_STATUS_NAMES.get(
                self.injury_status, self.injury_status
            ),
            "injury_status_raw": self.injury_status,
            "on_bye": self.on_bye,
            "is_playable": self.is_playable,
            "is_starting": self.is_starting,
            "projected_points": round(self.projected_points, 2),
            "effective_projection": round(self.effective_projection, 2),
            "actual_points": round(self.actual_points, 2),
            "season_projected_points": round(self.season_projected_points, 2),
            "season_actual_points": round(self.season_actual_points, 2),
            "source_points": {k: round(v, 2) for k, v in self.source_points.items()},
            "consensus_points": round(self.consensus_points, 2),
            "consensus_spread": round(self.consensus_spread, 2),
            "espn_projected_points": round(self.espn_projected_points, 2),
            "ros_points": round(self.ros_points, 2),
            "ros_games": self.ros_games,
            "ros_source_points": {k: round(v, 1) for k, v in self.ros_source_points.items()},
            "percent_owned": round(self.percent_owned, 1),
            "percent_started": round(self.percent_started, 1),
            "availability": self.availability,
            "fantasy_team_id": self.fantasy_team_id,
            "acquisition_type": self.acquisition_type,
        }


# Build a Player from an ESPN roster entry, which wraps the player record in
# a playerPoolEntry and carries the current lineup slot alongside it.
def player_from_roster_entry(entry, week, season, bye_weeks=None, final_week=None):
    pool_entry = entry.get("playerPoolEntry") or {}
    raw = pool_entry.get("player") or entry.get("player") or {}

    player = player_from_raw(
        raw, week, season, bye_weeks=bye_weeks, final_week=final_week
    )
    player.lineup_slot = entry.get("lineupSlotId", C.BENCH_SLOT)
    player.fantasy_team_id = pool_entry.get("onTeamId", player.fantasy_team_id)
    player.acquisition_type = entry.get("acquisitionType") or ""

    if pool_entry.get("status"):
        player.availability = pool_entry["status"]

    return player


# Build a Player from a bare ESPN player record, the shape returned by the
# player-pool views used for free agents and waiver targets.
def player_from_raw(raw, week, season, bye_weeks=None, final_week=None):
    stats = raw.get("stats") or []
    ownership = raw.get("ownership") or {}
    position_id = raw.get("defaultPositionId", 0)
    pro_team_id = raw.get("proTeamId", 0)

    projected = extract_week_points(stats, week, C.STAT_SOURCE_PROJECTED, season)
    actual = extract_week_points(stats, week, C.STAT_SOURCE_ACTUAL, season)
    ros = None
    if final_week:
        ros = extract_ros_points(stats, season, week, final_week)

    bye_weeks = bye_weeks or {}
    on_bye = bye_weeks.get(pro_team_id) == week

    return Player(
        player_id=raw.get("id", 0),
        name=raw.get("fullName") or raw.get("name") or "Unknown",
        position=C.position_name(position_id),
        pro_team=C.pro_team_abbrev(pro_team_id),
        eligible_slots=list(raw.get("eligibleSlots") or []),
        injury_status=raw.get("injuryStatus") or "ACTIVE",
        percent_owned=float(ownership.get("percentOwned") or 0.0),
        percent_started=float(ownership.get("percentStarted") or 0.0),
        projected_points=float(projected or 0.0),
        actual_points=float(actual or 0.0),
        season_projected_points=float(
            extract_season_points(stats, season, C.STAT_SOURCE_PROJECTED) or 0.0
        ),
        season_actual_points=float(
            extract_season_points(stats, season, C.STAT_SOURCE_ACTUAL) or 0.0
        ),
        on_bye=on_bye,
        espn_projected_points=float(projected or 0.0),
        ros_points=ros[0] if ros else 0.0,
        ros_games=ros[1] if ros else 0,
    )


@dataclass
class Team:
    """One fantasy team in the league, with its current roster."""

    team_id: int
    name: str
    abbrev: str = ""
    owner_names: list = field(default_factory=list)
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    standing: int = 0
    waiver_priority: int = 0
    faab_remaining: float = 0.0
    roster: list = field(default_factory=list)

    @property
    def record(self):
        if self.ties:
            return f"{self.wins}-{self.losses}-{self.ties}"
        return f"{self.wins}-{self.losses}"

    @property
    def starters(self):
        return [p for p in self.roster if p.is_starting]

    @property
    def bench(self):
        return [p for p in self.roster if p.lineup_slot == C.BENCH_SLOT]

    # What the current lineup is projected to score this week, as set.
    @property
    def projected_starting_points(self):
        return sum(p.effective_projection for p in self.starters)

    def player_by_id(self, player_id):
        for player in self.roster:
            if player.player_id == player_id:
                return player
        return None

    # Count of rostered players at each position, for surplus analysis.
    def position_counts(self):
        counts = {}
        for player in self.roster:
            counts[player.position] = counts.get(player.position, 0) + 1
        return counts

    def to_dict(self, include_roster=True):
        data = {
            "team_id": self.team_id,
            "name": self.name,
            "abbrev": self.abbrev,
            "owner_names": list(self.owner_names),
            "record": self.record,
            "wins": self.wins,
            "losses": self.losses,
            "ties": self.ties,
            "points_for": round(self.points_for, 2),
            "points_against": round(self.points_against, 2),
            "standing": self.standing,
            "waiver_priority": self.waiver_priority,
            "faab_remaining": self.faab_remaining,
            "projected_starting_points": round(self.projected_starting_points, 2),
            "position_counts": self.position_counts(),
        }
        if include_roster:
            data["roster"] = [p.to_dict() for p in self.roster]
        return data


@dataclass
class Matchup:
    """One head-to-head pairing in a given week."""

    week: int
    home_team_id: int
    away_team_id: int
    home_score: float = 0.0
    away_score: float = 0.0
    matchup_id: int = 0

    # The other side of this matchup for a given team, or None.
    def opponent_of(self, team_id):
        if team_id == self.home_team_id:
            return self.away_team_id
        if team_id == self.away_team_id:
            return self.home_team_id
        return None

    def to_dict(self):
        return {
            "week": self.week,
            "matchup_id": self.matchup_id,
            "home_team_id": self.home_team_id,
            "away_team_id": self.away_team_id,
            "home_score": round(self.home_score, 2),
            "away_score": round(self.away_score, 2),
        }


@dataclass
class LeagueSettings:
    """League name, size, scoring type, and the starting lineup shape."""

    name: str = "League"
    size: int = 0
    scoring_type: str = ""
    current_week: int = 1
    final_week: int = 17
    uses_faab: bool = False
    faab_budget: float = 0.0
    lineup_slot_counts: dict = field(default_factory=dict)
    roster_size: int = 0
    # ESPN's raw scoringSettings block, used to score other sources' stat
    # lines the way this league does.
    scoring_settings: dict = field(default_factory=dict)

    # Roster spots that count against the roster limit. ESPN keeps injured
    # reserve outside that limit, so those slots do not count here.
    @property
    def active_roster_size(self):
        return sum(
            count
            for slot_id, count in self.lineup_slot_counts.items()
            if slot_id != C.IR_SLOT
        )

    # The starting slots, expanded one entry per slot, e.g. [0, 2, 2, 4, 4, 23].
    def starting_slots(self):
        slots = []
        for slot_id, count in sorted(self.lineup_slot_counts.items()):
            if slot_id in C.NON_SCORING_SLOTS:
                continue
            slots.extend([slot_id] * int(count))
        return slots

    def to_dict(self):
        return {
            "name": self.name,
            "size": self.size,
            "scoring_type": self.scoring_type,
            "current_week": self.current_week,
            "final_week": self.final_week,
            "uses_faab": self.uses_faab,
            "faab_budget": self.faab_budget,
            "roster_size": self.roster_size,
            "active_roster_size": self.active_roster_size,
            "starting_slots": [
                {"slot_id": s, "slot_name": C.slot_name(s)} for s in self.starting_slots()
            ],
        }


@dataclass
class League:
    """Everything we loaded about the league for one point in time."""

    settings: LeagueSettings
    teams: list = field(default_factory=list)
    matchups: list = field(default_factory=list)
    week: int = 1

    def team_by_id(self, team_id):
        for team in self.teams:
            if team.team_id == team_id:
                return team
        return None

    # The matchup a team is playing in a given week, or None on a bye.
    def matchup_for(self, team_id, week=None):
        week = week or self.week
        for matchup in self.matchups:
            if matchup.week != week:
                continue
            if team_id in (matchup.home_team_id, matchup.away_team_id):
                return matchup
        return None

    def opponent_for(self, team_id, week=None):
        matchup = self.matchup_for(team_id, week)
        if not matchup:
            return None
        return self.team_by_id(matchup.opponent_of(team_id))

    # Every player rostered anywhere in the league, keyed by player id.
    def all_rostered_player_ids(self):
        ids = set()
        for team in self.teams:
            for player in team.roster:
                ids.add(player.player_id)
        return ids
