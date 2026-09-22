"""
Turns raw ESPN payloads into League objects, and caches them.

ESPN's league resource is one big document containing settings, teams,
rosters, and the schedule. This module parses that document, handles the
older and newer shapes ESPN has used for team names and owners, and keeps a
short-lived cache so a page full of widgets does not refetch ten times.
"""

import time

from . import constants as C
from .espn_client import EspnClient
from .models import (
    League,
    LeagueSettings,
    Matchup,
    Team,
    player_from_roster_entry,
    player_from_raw,
)


# ESPN has used two shapes for team names over the years: a single "name"
# field, and a location/nickname pair. Handle both.
def parse_team_name(raw_team):
    name = (raw_team.get("name") or "").strip()
    if name:
        return name

    location = (raw_team.get("location") or "").strip()
    nickname = (raw_team.get("nickname") or "").strip()
    combined = f"{location} {nickname}".strip()
    return combined or f"Team {raw_team.get('id', '?')}"


# Map member GUIDs on a team to the display names in the league's member list.
def parse_owner_names(raw_team, members_by_id):
    names = []
    for owner_id in raw_team.get("owners") or []:
        member = members_by_id.get(owner_id)
        if not member:
            continue
        display = (member.get("displayName") or "").strip()
        first = (member.get("firstName") or "").strip()
        last = (member.get("lastName") or "").strip()
        full = f"{first} {last}".strip()
        names.append(full or display or owner_id)
    return names


# Pull the starting lineup shape and league-wide rules out of the settings
# block, falling back to sane values when a field is absent.
def parse_settings(payload):
    raw = payload.get("settings") or {}
    roster = raw.get("rosterSettings") or {}
    acquisition = raw.get("acquisitionSettings") or {}
    scoring = raw.get("scoringSettings") or {}
    schedule = raw.get("scheduleSettings") or {}
    status = payload.get("status") or {}

    slot_counts = {}
    for key, value in (roster.get("lineupSlotCounts") or {}).items():
        try:
            slot_counts[int(key)] = int(value)
        except (TypeError, ValueError):
            continue

    roster_size = sum(slot_counts.values())

    scoring_type = scoring.get("scoringType") or ""
    # ESPN reports PPR through a per-reception scoring item, not a flag.
    if not scoring_type:
        for item in scoring.get("scoringItems") or []:
            if item.get("statId") == 53 and item.get("points"):
                scoring_type = "PPR" if item["points"] >= 1 else "FRACTIONAL_PPR"
                break

    current_week = (
        payload.get("scoringPeriodId")
        or status.get("latestScoringPeriod")
        or status.get("currentMatchupPeriod")
        or 1
    )

    return LeagueSettings(
        name=raw.get("name") or "Fantasy League",
        size=raw.get("size") or len(payload.get("teams") or []),
        scoring_type=scoring_type or "STANDARD",
        current_week=int(current_week),
        final_week=int(
            status.get("finalScoringPeriod")
            or schedule.get("matchupPeriodCount")
            or 17
        ),
        uses_faab=bool(acquisition.get("isUsingAcquisitionBudget")),
        faab_budget=float(acquisition.get("acquisitionBudget") or 0.0),
        lineup_slot_counts=slot_counts,
        roster_size=roster_size,
    )


# Build one Team, including its roster for the requested week.
def parse_team(raw_team, week, season, members_by_id, bye_weeks):
    record = (raw_team.get("record") or {}).get("overall") or {}
    counter = raw_team.get("transactionCounter") or {}

    roster_entries = (raw_team.get("roster") or {}).get("entries") or []
    roster = []
    for entry in roster_entries:
        player = player_from_roster_entry(entry, week, season, bye_weeks=bye_weeks)
        player.fantasy_team_id = raw_team.get("id", 0)
        roster.append(player)

    team = Team(
        team_id=raw_team.get("id", 0),
        name=parse_team_name(raw_team),
        abbrev=raw_team.get("abbrev") or "",
        owner_names=parse_owner_names(raw_team, members_by_id),
        wins=int(record.get("wins") or 0),
        losses=int(record.get("losses") or 0),
        ties=int(record.get("ties") or 0),
        points_for=float(record.get("pointsFor") or 0.0),
        points_against=float(record.get("pointsAgainst") or 0.0),
        standing=int(raw_team.get("playoffSeed") or 0),
        waiver_priority=int(raw_team.get("waiverRank") or 0),
        roster=roster,
    )

    return team, float(counter.get("acquisitionBudgetSpent") or 0.0)


# Build the schedule. ESPN keys matchups by matchup period, which equals the
# scoring period in a standard weekly league.
def parse_matchups(payload):
    matchups = []
    for raw in payload.get("schedule") or []:
        home = raw.get("home") or {}
        away = raw.get("away") or {}
        if not home and not away:
            continue

        matchups.append(
            Matchup(
                week=int(raw.get("matchupPeriodId") or 0),
                matchup_id=int(raw.get("id") or 0),
                home_team_id=int(home.get("teamId") or 0),
                away_team_id=int(away.get("teamId") or 0),
                home_score=float(home.get("totalPoints") or 0.0),
                away_score=float(away.get("totalPoints") or 0.0),
            )
        )
    return matchups


# Assemble the whole League from one snapshot payload.
def parse_league(payload, week=None, season=None, bye_weeks=None):
    settings = parse_settings(payload)
    week = int(week or settings.current_week)
    season = int(season or payload.get("seasonId") or 0)
    bye_weeks = bye_weeks or {}

    members_by_id = {}
    for member in payload.get("members") or []:
        members_by_id[member.get("id")] = member

    teams = []
    for raw_team in payload.get("teams") or []:
        team, budget_spent = parse_team(
            raw_team, week, season, members_by_id, bye_weeks
        )
        if settings.uses_faab:
            team.faab_remaining = max(0.0, settings.faab_budget - budget_spent)
        teams.append(team)

    teams.sort(key=lambda t: (t.standing or 99, -t.points_for))

    return League(
        settings=settings,
        teams=teams,
        matchups=parse_matchups(payload),
        week=week,
    )


class LeagueService:
    """Loads and caches league state for the configured league."""

    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or EspnClient(settings)
        self._cache = {}

    def close(self):
        self.client.close()

    # Return a cached value if it is still fresh, otherwise compute it.
    def _cached(self, key, producer):
        ttl = self.settings.cache_ttl_seconds
        now = time.monotonic()

        hit = self._cache.get(key)
        if hit and ttl > 0 and (now - hit[0]) < ttl:
            return hit[1]

        value = producer()
        self._cache[key] = (now, value)
        return value

    def invalidate(self):
        """Drop cached state, e.g. right after a roster move lands."""
        self._cache.clear()

    def bye_weeks(self):
        return self._cached("byes", self.client.fetch_bye_weeks)

    # The full league for a week. Week None means whatever ESPN calls current.
    def load_league(self, week=None):
        def producer():
            payload = self.client.fetch_league_snapshot(week=week)
            return parse_league(
                payload,
                week=week,
                season=self.settings.season,
                bye_weeks=self.bye_weeks(),
            )

        return self._cached(("league", week), producer)

    def current_week(self):
        return self.load_league().week

    # The team configured as "mine". Falls back to the only team owned by the
    # SWID member when ESPN_TEAM_ID is not set.
    def my_team(self, week=None):
        league = self.load_league(week)

        if self.settings.team_id:
            team = league.team_by_id(self.settings.team_id)
            if team:
                return team

        if self.settings.swid:
            for team in league.teams:
                if any(self.settings.swid in name for name in team.owner_names):
                    return team

        return league.teams[0] if league.teams else None

    # Available players, as Player objects rather than raw ESPN records.
    def free_agents(self, week=None, limit=150, slot_ids=None, positions=None):
        league = self.load_league(week)
        week = league.week

        if positions and not slot_ids:
            slot_ids = []
            for position in positions:
                slot_ids.extend(C.FALLBACK_ELIGIBLE_SLOTS.get(position.upper(), []))
            slot_ids = sorted({s for s in slot_ids if s not in C.NON_SCORING_SLOTS})

        key = ("fa", week, limit, tuple(slot_ids or ()))

        def producer():
            raw_players = self.client.fetch_free_agents(
                week, limit=limit, slot_ids=slot_ids
            )
            byes = self.bye_weeks()

            players = []
            for record in raw_players:
                raw = record.get("player") or record
                player = player_from_raw(
                    raw, week, self.settings.season, bye_weeks=byes
                )
                player.availability = record.get("status") or C.STATUS_FREEAGENT
                players.append(player)
            return players

        return self._cached(key, producer)

    def pending_transactions(self):
        return self._cached("pending", self.client.fetch_pending_transactions)


# Find a team by whatever the user typed: a team id, part of the team name,
# or part of an owner's name. Returns (team, candidates). When the match is
# ambiguous the team is None and candidates holds everything that matched,
# so the caller can ask which one was meant.
def find_team(league, query):
    text = str(query).strip().lower()
    if not text:
        return None, []

    # A bare number is a team id.
    if text.isdigit():
        team = league.team_by_id(int(text))
        return (team, [team]) if team else (None, [])

    exact = []
    partial = []
    for team in league.teams:
        names = [team.name, team.abbrev] + list(team.owner_names)
        lowered = [n.lower() for n in names if n]

        if any(text == name for name in lowered):
            exact.append(team)
        elif any(text in name for name in lowered):
            partial.append(team)

        # Match a first or last name on its own, which is how people refer
        # to each other in a league.
        elif any(text in name.split() for name in lowered):
            partial.append(team)

    matches = exact or partial
    if len(matches) == 1:
        return matches[0], matches
    return None, matches
