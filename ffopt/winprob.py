"""
Score distributions and win probability.

A lineup's weekly score is modelled as a normal distribution. The mean is the
sum of its starters' projections. The spread comes from the starters
themselves: each player has their own week-to-week uncertainty, and the
lineup's variance is the sum of theirs. That makes a lineup of boom-or-bust
receivers genuinely riskier than a lineup of steady running backs with the
same projected total, which is what lets the optimizer trade points for
win probability.
"""

import math


# Typical week-to-week spread of a whole fantasy team's score. Used when a
# lineup's own spread is unknown, such as an opponent with no roster loaded.
WEEKLY_SCORE_STDDEV = 27.0

# A player's spread as a share of their projection, by position. Steadier
# roles (QB, RB) sit lower; touchdown-dependent ones (WR, TE, D/ST) sit higher.
# These are the fallback when no measured spread is available; see nflverse.py.
POSITION_STDDEV_RATIO = {
    "QB": 0.45,
    "RB": 0.65,
    "WR": 0.75,
    "TE": 0.75,
    "K": 0.55,
    "D/ST": 0.70,
}
DEFAULT_STDDEV_RATIO = 0.70

# Even a low projection carries some uncertainty.
MIN_PLAYER_STDDEV = 2.0


# Week-to-week spread of one player's score. A player projected for nothing
# (bye, ruled out) has no spread, because they are going to score nothing.
def player_stddev(player, projection=None):
    if projection is None:
        projection = player.effective_projection
    if projection <= 0:
        return 0.0
    ratio = player.stddev_ratio or POSITION_STDDEV_RATIO.get(
        player.position, DEFAULT_STDDEV_RATIO
    )
    # When projection sources disagree, the true average itself is less
    # certain, which widens the spread on top of normal game-to-game swings.
    spread = math.hypot(ratio * projection, player.consensus_spread)
    return max(MIN_PLAYER_STDDEV, spread)


# Mean and spread of a set of starters, assuming their scores are independent.
def lineup_distribution(players, projection_fn=None):
    mean = 0.0
    variance = 0.0
    for player in players:
        projection = projection_fn(player) if projection_fn else player.effective_projection
        mean += projection
        variance += player_stddev(player, projection) ** 2
    return mean, math.sqrt(variance)


# Probability the first score beats the second, assuming both are normally
# distributed. Without a spread for a side, the league-typical spread is used.
def win_probability(mean_for, mean_against, stddev_for=None, stddev_against=None):
    if stddev_for is None:
        stddev_for = WEEKLY_SCORE_STDDEV
    if stddev_against is None:
        stddev_against = WEEKLY_SCORE_STDDEV
    combined = math.sqrt(stddev_for**2 + stddev_against**2)
    if combined == 0:
        if mean_for == mean_against:
            return 0.5
        return 1.0 if mean_for > mean_against else 0.0
    z = (mean_for - mean_against) / combined
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
