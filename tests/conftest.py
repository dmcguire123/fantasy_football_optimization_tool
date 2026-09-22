"""
Shared fixtures: a synthetic ESPN league payload and a client wired to it.

No test touches the network. The payload below mimics the shape ESPN returns
for a twelve-team PPR league, including the quirks the parser has to cope
with: stat lines for several weeks and sources, and slot eligibility lists.
"""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffopt.config import Settings
from ffopt.espn_client import EspnClient
from ffopt.league import LeagueService


WEEK = 5
SEASON = 2025

# Slot eligibility as ESPN reports it, per position.
ELIGIBLE = {
    "QB": [0, 7, 20, 21],
    "RB": [2, 3, 23, 7, 20, 21],
    "WR": [4, 3, 5, 23, 7, 20, 21],
    "TE": [6, 5, 23, 7, 20, 21],
    "K": [17, 20, 21],
    "D/ST": [16, 20, 21],
}

POSITION_IDS = {"QB": 1, "RB": 2, "WR": 3, "TE": 4, "K": 5, "D/ST": 16}


# Build one ESPN player record with a weekly projection and a season total.
def make_player(
    player_id,
    name,
    position,
    projected,
    pro_team_id=12,
    injury="ACTIVE",
    season_projected=None,
    percent_owned=50.0,
):
    season_projected = season_projected if season_projected is not None else projected * 14

    return {
        "id": player_id,
        "fullName": name,
        "defaultPositionId": POSITION_IDS[position],
        "proTeamId": pro_team_id,
        "eligibleSlots": ELIGIBLE[position],
        "injuryStatus": injury,
        "ownership": {"percentOwned": percent_owned, "percentStarted": percent_owned - 10},
        "stats": [
            {
                "scoringPeriodId": WEEK,
                "statSourceId": 1,
                "statSplitTypeId": 1,
                "appliedTotal": projected,
            },
            {
                "scoringPeriodId": WEEK,
                "statSourceId": 0,
                "statSplitTypeId": 1,
                "appliedTotal": 0.0,
            },
            {
                "scoringPeriodId": WEEK - 1,
                "statSourceId": 1,
                "statSplitTypeId": 1,
                "appliedTotal": projected + 3,
            },
            {
                "seasonId": SEASON,
                "scoringPeriodId": 0,
                "statSourceId": 1,
                "statSplitTypeId": 0,
                "appliedTotal": season_projected,
            },
            {
                "seasonId": SEASON,
                "scoringPeriodId": 0,
                "statSourceId": 0,
                "statSplitTypeId": 0,
                "appliedTotal": projected * 4,
            },
        ],
    }


# Wrap a player record as a roster entry in a given lineup slot.
def roster_entry(player_record, lineup_slot, team_id):
    return {
        "lineupSlotId": lineup_slot,
        "acquisitionType": "DRAFT",
        "playerId": player_record["id"],
        "playerPoolEntry": {
            "id": player_record["id"],
            "onTeamId": team_id,
            "status": "ONTEAM",
            "player": player_record,
        },
    }


# Roster for team 1, deliberately misconfigured: the best RB is benched and a
# player on bye is in the starting lineup, so the optimizer has work to do.
def team_one_entries():
    return [
        roster_entry(make_player(101, "Ace Passer", "QB", 21.0), 0, 1),
        roster_entry(make_player(102, "Bench Burner", "RB", 18.0), 20, 1),
        roster_entry(make_player(103, "Starting Back", "RB", 9.0), 2, 1),
        roster_entry(make_player(104, "Bye Week Back", "RB", 12.0, pro_team_id=9), 2, 1),
        roster_entry(make_player(105, "Wideout One", "WR", 14.0), 4, 1),
        roster_entry(make_player(106, "Wideout Two", "WR", 11.0), 4, 1),
        roster_entry(make_player(107, "Wideout Three", "WR", 10.5), 20, 1),
        roster_entry(make_player(108, "Tight Guy", "TE", 8.0), 6, 1),
        roster_entry(make_player(109, "Flex Filler", "WR", 6.0), 23, 1),
        roster_entry(make_player(110, "Kick It", "K", 7.5), 17, 1),
        roster_entry(make_player(111, "The Defense", "D/ST", 6.5), 16, 1),
        roster_entry(make_player(112, "Hurt Backup", "RB", 5.0, injury="OUT"), 20, 1),
        roster_entry(make_player(113, "Deep Stash", "WR", 2.0), 20, 1),
        roster_entry(make_player(114, "Shelved Star", "RB", 0.0, injury="INJURY_RESERVE"), 21, 1),
    ]


# Roster for team 2, the opponent. Balanced and correctly set.
def team_two_entries():
    return [
        roster_entry(make_player(201, "Rival Passer", "QB", 19.0), 0, 2),
        roster_entry(make_player(202, "Rival Back One", "RB", 15.0), 2, 2),
        roster_entry(make_player(203, "Rival Back Two", "RB", 13.0), 2, 2),
        roster_entry(make_player(204, "Rival Wide One", "WR", 16.0), 4, 2),
        roster_entry(make_player(205, "Rival Wide Two", "WR", 12.0), 4, 2),
        roster_entry(make_player(206, "Rival Tight", "TE", 9.0), 6, 2),
        roster_entry(make_player(207, "Rival Flex", "WR", 11.0), 23, 2),
        roster_entry(make_player(208, "Rival Kicker", "K", 8.0), 17, 2),
        roster_entry(make_player(209, "Rival Defense", "D/ST", 7.0), 16, 2),
        roster_entry(make_player(210, "Rival Bench RB", "RB", 10.0), 20, 2),
        roster_entry(make_player(211, "Rival Bench WR", "WR", 4.0), 20, 2),
    ]


# Team 3 exists to give the league a surplus of running backs for the trade
# and power ranking tests.
def team_three_entries():
    return [
        roster_entry(make_player(301, "Third Passer", "QB", 14.0), 0, 3),
        roster_entry(make_player(302, "Deep Back A", "RB", 17.0), 2, 3),
        roster_entry(make_player(303, "Deep Back B", "RB", 16.0), 2, 3),
        roster_entry(make_player(304, "Deep Back C", "RB", 15.5), 23, 3),
        roster_entry(make_player(305, "Deep Back D", "RB", 14.5), 20, 3),
        roster_entry(make_player(306, "Deep Back E", "RB", 13.5), 20, 3),
        roster_entry(make_player(307, "Thin Wide A", "WR", 7.0), 4, 3),
        roster_entry(make_player(308, "Thin Wide B", "WR", 5.0), 4, 3),
        roster_entry(make_player(309, "Third Tight", "TE", 6.0), 6, 3),
        roster_entry(make_player(310, "Third Kicker", "K", 7.0), 17, 3),
        roster_entry(make_player(311, "Third Defense", "D/ST", 5.5), 16, 3),
    ]


def league_payload():
    return {
        "id": 123456,
        "seasonId": SEASON,
        "scoringPeriodId": WEEK,
        "status": {
            "currentMatchupPeriod": WEEK,
            "latestScoringPeriod": WEEK,
            "finalScoringPeriod": 17,
        },
        "settings": {
            "name": "Test Gridiron League",
            "size": 3,
            "scoringSettings": {
                "scoringType": "",
                "scoringItems": [{"statId": 53, "points": 1.0}],
            },
            "rosterSettings": {
                "lineupSlotCounts": {
                    "0": 1, "2": 2, "4": 2, "6": 1, "23": 1,
                    "16": 1, "17": 1, "20": 4, "21": 1,
                }
            },
            "acquisitionSettings": {
                "isUsingAcquisitionBudget": True,
                "acquisitionBudget": 100,
            },
            "scheduleSettings": {"matchupPeriodCount": 14},
        },
        "members": [
            {"id": "{OWNER-1}", "displayName": "manager1", "firstName": "Dana", "lastName": "Reed"},
            {"id": "{OWNER-2}", "displayName": "manager2", "firstName": "Sam", "lastName": "Cole"},
            {"id": "{OWNER-3}", "displayName": "manager3", "firstName": "Darren", "lastName": "Park"},
        ],
        "teams": [
            {
                "id": 1, "name": "My Squad", "abbrev": "MINE", "owners": ["{OWNER-1}"],
                "playoffSeed": 2, "waiverRank": 4,
                "record": {"overall": {"wins": 3, "losses": 1, "ties": 0,
                                       "pointsFor": 420.5, "pointsAgainst": 390.0}},
                "transactionCounter": {"acquisitionBudgetSpent": 25},
                "roster": {"entries": team_one_entries()},
            },
            {
                "id": 2, "name": "Rival Crew", "abbrev": "RIVL", "owners": ["{OWNER-2}"],
                "playoffSeed": 1, "waiverRank": 1,
                "record": {"overall": {"wins": 4, "losses": 0, "ties": 0,
                                       "pointsFor": 460.0, "pointsAgainst": 380.0}},
                "transactionCounter": {"acquisitionBudgetSpent": 10},
                "roster": {"entries": team_two_entries()},
            },
            {
                "id": 3, "location": "Deep", "nickname": "Backfield", "abbrev": "DEEP",
                "owners": ["{OWNER-3}"], "playoffSeed": 3, "waiverRank": 8,
                "record": {"overall": {"wins": 1, "losses": 3, "ties": 0,
                                       "pointsFor": 360.0, "pointsAgainst": 430.0}},
                "transactionCounter": {"acquisitionBudgetSpent": 60},
                "roster": {"entries": team_three_entries()},
            },
        ],
        "schedule": [
            {"id": 1, "matchupPeriodId": WEEK,
             "home": {"teamId": 1, "totalPoints": 0.0},
             "away": {"teamId": 2, "totalPoints": 0.0}},
            {"id": 2, "matchupPeriodId": WEEK - 1,
             "home": {"teamId": 1, "totalPoints": 110.0},
             "away": {"teamId": 3, "totalPoints": 98.0}},
        ],
        "pendingTransactions": [
            {"id": "tx1", "type": "WAIVER", "teamId": 1, "bidAmount": 7,
             "items": [{"playerId": 401, "type": "ADD"}]}
        ],
    }


def free_agent_payload():
    return {
        "players": [
            {"id": 401, "status": "FREEAGENT",
             "player": make_player(401, "Waiver Gem RB", "RB", 13.5, percent_owned=31.0)},
            {"id": 402, "status": "FREEAGENT",
             "player": make_player(402, "Streaming QB", "QB", 17.0, percent_owned=22.0)},
            {"id": 403, "status": "WAIVERS",
             "player": make_player(403, "Handcuff WR", "WR", 9.5, percent_owned=12.0)},
            {"id": 404, "status": "FREEAGENT",
             "player": make_player(404, "Deep Sleeper TE", "TE", 3.0, percent_owned=2.0)},
        ]
    }


def pro_teams_payload():
    # Green Bay (id 9) is on bye in the test week, which matters for player 104.
    return {
        "settings": {
            "proTeams": [
                {"id": 12, "abbrev": "KC", "byeWeek": 10},
                {"id": 9, "abbrev": "GB", "byeWeek": WEEK},
            ]
        }
    }


# Route every ESPN URL the client knows about to the fixtures above.
def make_transport(record=None):
    def handler(request):
        url = str(request.url)

        if record is not None:
            record.append(request)

        if request.method == "POST" and "/transactions/" in url:
            return httpx.Response(200, json={"status": "OK", "id": "tx-new"})

        if "proTeamSchedules_wl" in url:
            return httpx.Response(200, json=pro_teams_payload())

        if "kona_player_info" in url:
            return httpx.Response(200, json=free_agent_payload())

        return httpx.Response(200, json=league_payload())

    return httpx.MockTransport(handler)


@pytest.fixture
def settings():
    return Settings(
        league_id="123456",
        season=SEASON,
        team_id=1,
        swid="{OWNER-1}",
        espn_s2="cookie-value",
        cache_ttl_seconds=0.0,
    )


@pytest.fixture
def requests_log():
    return []


@pytest.fixture
def client(settings, requests_log):
    espn = EspnClient(settings, transport=make_transport(requests_log))
    yield espn
    espn.close()


@pytest.fixture
def service(settings, client):
    return LeagueService(settings, client=client)


@pytest.fixture
def league(service):
    return service.load_league()


@pytest.fixture
def starting_slots(league):
    return league.settings.starting_slots()
