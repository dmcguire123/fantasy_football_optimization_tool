"""
Static ESPN fantasy football identifiers.

ESPN's API speaks in integer ids for everything: lineup slots, positions,
pro teams, injury states. These tables translate them into names we can show
to a human. They are stable across seasons.
"""

# Lineup slot ids. Slot 20 is the bench, 21 is injured reserve, and
# everything else is a startable spot. FLEX (23) is the common RB/WR/TE spot.
SLOT_NAMES = {
    0: "QB",
    1: "TQB",
    2: "RB",
    3: "RB/WR",
    4: "WR",
    5: "WR/TE",
    6: "TE",
    7: "OP",
    8: "DT",
    9: "DE",
    10: "LB",
    11: "DL",
    12: "CB",
    13: "S",
    14: "DB",
    15: "DP",
    16: "D/ST",
    17: "K",
    18: "P",
    19: "HC",
    20: "BE",
    21: "IR",
    23: "FLEX",
    24: "EDR",
}

BENCH_SLOT = 20
IR_SLOT = 21

# Slots that do not score for you in a given week.
NON_SCORING_SLOTS = {BENCH_SLOT, IR_SLOT}


# Primary position ids, from player.defaultPositionId.
POSITION_NAMES = {
    1: "QB",
    2: "RB",
    3: "WR",
    4: "TE",
    5: "K",
    7: "P",
    9: "DT",
    10: "DE",
    11: "LB",
    12: "CB",
    13: "S",
    14: "DB",
    16: "D/ST",
}


# Pro team ids, from player.proTeamId. 0 means free agent / no team.
PRO_TEAM_ABBREVS = {
    0: "FA",
    1: "ATL",
    2: "BUF",
    3: "CHI",
    4: "CIN",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GB",
    10: "TEN",
    11: "IND",
    12: "KC",
    13: "LV",
    14: "LAR",
    15: "MIA",
    16: "MIN",
    17: "NE",
    18: "NO",
    19: "NYG",
    20: "NYJ",
    21: "PHI",
    22: "ARI",
    23: "PIT",
    24: "LAC",
    25: "SF",
    26: "SEA",
    27: "TB",
    28: "WSH",
    29: "CAR",
    30: "JAX",
    33: "BAL",
    34: "HOU",
}


# player.injuryStatus values worth surfacing. ACTIVE is the normal case.
INJURY_STATUS_NAMES = {
    "ACTIVE": "Active",
    "NORMAL": "Active",
    "QUESTIONABLE": "Questionable",
    "DOUBTFUL": "Doubtful",
    "OUT": "Out",
    "INJURY_RESERVE": "IR",
    "SUSPENSION": "Suspended",
    "DAY_TO_DAY": "Day to day",
    "PROBABLE": "Probable",
}

# Injury states where the player is very unlikely to play. The optimizer
# treats these as zero-projection unless the caller says otherwise.
UNAVAILABLE_INJURY_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL"}


# player.stats entries carry these discriminators.
STAT_SOURCE_ACTUAL = 0
STAT_SOURCE_PROJECTED = 1

STAT_SPLIT_SEASON = 0
STAT_SPLIT_WEEK = 1


# Roster availability status, used when filtering the player pool.
STATUS_FREEAGENT = "FREEAGENT"
STATUS_WAIVERS = "WAIVERS"
STATUS_ONTEAM = "ONTEAM"


# Fallback slot eligibility, used only when ESPN omits eligibleSlots for a
# player. Normally we trust the eligibleSlots list on the player record.
FALLBACK_ELIGIBLE_SLOTS = {
    "QB": [0, 7, 20, 21],
    "RB": [2, 3, 23, 7, 20, 21],
    "WR": [4, 3, 5, 23, 7, 20, 21],
    "TE": [6, 5, 23, 7, 20, 21],
    "K": [17, 20, 21],
    "D/ST": [16, 20, 21],
}


def slot_name(slot_id: int) -> str:
    """Human name for a lineup slot id."""
    return SLOT_NAMES.get(slot_id, f"SLOT{slot_id}")


def position_name(position_id: int) -> str:
    """Human name for a primary position id."""
    return POSITION_NAMES.get(position_id, f"POS{position_id}")


def pro_team_abbrev(pro_team_id: int) -> str:
    """Team abbreviation for a pro team id."""
    return PRO_TEAM_ABBREVS.get(pro_team_id, "FA")
