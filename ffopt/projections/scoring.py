"""
Score any stat line with this league's own scoring rules.

ESPN hands us fantasy points already scored for the league. Other sources
hand us stat lines (yards, touchdowns, catches) or points in some generic
format. To compare them fairly, every stat line is turned into ESPN's stat
ids and then scored with the league's scoringItems, the same way ESPN does.

ESPN scores yardage either per yard (0.04 per passing yard) or per block of
yards (1 point per 25 passing yards). Both are handled: the block stats are
worked out from total yards.
"""


# ESPN stat ids for the stats projection sources give us. Checked against
# ESPN's own stat lines: a player's appliedTotal equals these stats times the
# league's points per stat.
PASS_ATT = 0
PASS_CMP = 1
PASS_INC = 2
PASS_YDS = 3
PASS_TD = 4
PASS_2PT = 19
PASS_INT = 20
RUSH_ATT = 23
RUSH_YDS = 24
RUSH_TD = 25
RUSH_2PT = 26
REC_YDS = 42
REC_TD = 43
REC_2PT = 44
RECEPTIONS = 53
TARGETS = 58
FUMBLES = 68
FUMBLES_LOST = 72

# "One point per N yards" stats, as (stat id, N), for each kind of yardage.
PASS_YARD_BLOCKS = [(5, 5), (6, 10), (7, 20), (8, 25), (9, 50), (10, 100)]
RUSH_YARD_BLOCKS = [(27, 5), (28, 10), (29, 20), (30, 25), (31, 50), (32, 100)]
REC_YARD_BLOCKS = [(47, 5), (48, 10), (49, 20), (50, 25), (51, 50), (52, 100)]

# Our plain stat names, and the ESPN stat id each one maps to.
STAT_IDS = {
    "pass_att": PASS_ATT,
    "pass_cmp": PASS_CMP,
    "pass_yds": PASS_YDS,
    "pass_td": PASS_TD,
    "pass_int": PASS_INT,
    "pass_2pt": PASS_2PT,
    "rush_att": RUSH_ATT,
    "rush_yds": RUSH_YDS,
    "rush_td": RUSH_TD,
    "rush_2pt": RUSH_2PT,
    "rec": RECEPTIONS,
    "rec_yds": REC_YDS,
    "rec_td": REC_TD,
    "rec_2pt": REC_2PT,
    "targets": TARGETS,
    "fumbles": FUMBLES,
    "fumbles_lost": FUMBLES_LOST,
}

# ESPN lets a league score a stat differently for one position, keyed by
# that position's lineup slot. TE premium is the common case.
POSITION_SLOTS = {"QB": 0, "RB": 2, "WR": 4, "TE": 6, "D/ST": 16, "K": 17}

# Offensive positions we score from stat lines. Kickers and defenses are
# taken from each source's standard-scoring points instead (see service.py).
STAT_SCORED_POSITIONS = {"QB", "RB", "WR", "TE"}


# The expected number of whole N-yard blocks in a projected yardage total.
# Projections are averages, and flooring an average would undercount, so
# take off the half block that flooring loses on average.
def expected_blocks(yards, block):
    if yards <= 0:
        return 0.0
    return max(0.0, yards / block - (block - 1) / (2 * block))


# Turn a stat line in our plain names into ESPN stat ids, including the
# derived stats ESPN scores: incompletions and per-N-yard blocks.
def to_espn_stats(named):
    stats = {}
    for name, value in named.items():
        stat_id = STAT_IDS.get(name)
        if stat_id is None or value is None:
            continue
        stats[stat_id] = float(value)

    if PASS_ATT in stats and PASS_CMP in stats:
        stats[PASS_INC] = max(0.0, stats[PASS_ATT] - stats[PASS_CMP])

    for yards_id, blocks in (
        (PASS_YDS, PASS_YARD_BLOCKS),
        (RUSH_YDS, RUSH_YARD_BLOCKS),
        (REC_YDS, REC_YARD_BLOCKS),
    ):
        yards = stats.get(yards_id, 0.0)
        for block_id, block in blocks:
            stats[block_id] = expected_blocks(yards, block)

    return stats


class LeagueScoring:
    """The league's points per stat, read from ESPN's scoringSettings."""

    def __init__(self, items):
        # stat id -> (default points, {slot id: points})
        self.items = items

    # Build from the scoringSettings block of the league payload.
    @classmethod
    def from_settings(cls, scoring_settings):
        items = {}
        for item in (scoring_settings or {}).get("scoringItems") or []:
            stat_id = item.get("statId")
            if stat_id is None:
                continue
            overrides = {}
            for slot, points in (item.get("pointsOverrides") or {}).items():
                try:
                    overrides[int(slot)] = float(points)
                except (TypeError, ValueError):
                    continue
            items[int(stat_id)] = (float(item.get("points") or 0.0), overrides)
        return cls(items)

    # Plain full-PPR scoring, for use before a league has been loaded.
    @classmethod
    def default_ppr(cls):
        items = {
            PASS_YDS: 0.04,
            PASS_TD: 4.0,
            PASS_INT: -2.0,
            PASS_2PT: 2.0,
            RUSH_YDS: 0.1,
            RUSH_TD: 6.0,
            RUSH_2PT: 2.0,
            RECEPTIONS: 1.0,
            REC_YDS: 0.1,
            REC_TD: 6.0,
            REC_2PT: 2.0,
            FUMBLES_LOST: -2.0,
        }
        return cls({stat_id: (points, {}) for stat_id, points in items.items()})

    # What one unit of a stat is worth for a player at this position.
    def points_for(self, stat_id, position):
        points, overrides = self.items.get(stat_id, (0.0, {}))
        slot = POSITION_SLOTS.get(position)
        if slot in overrides:
            return overrides[slot]
        return points

    # Score a stat line keyed by ESPN stat id.
    def score(self, espn_stats, position):
        total = 0.0
        for stat_id, value in espn_stats.items():
            total += self.points_for(int(stat_id), position) * float(value)
        return total

    # Score a stat line in our plain names.
    def score_named(self, named, position):
        return self.score(to_espn_stats(named), position)

    # Points per reception, which decides which generic-format number
    # (standard, half, full PPR) is the closest stand-in for this league.
    def reception_points(self, position="WR"):
        return self.points_for(RECEPTIONS, position)
