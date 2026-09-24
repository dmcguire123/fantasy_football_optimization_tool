"""
Blend several sources' projections into one number.

Twelve seasons of accuracy data from FantasyFootballAnalytics show a plain
average of sources beats nearly every single source, and that weighting
sources by past accuracy does not help, because which source is most
accurate changes from year to year. Our own backtest (backtest.py) agrees:
the ESPN and Sleeper average beats each of them at every position. So the
experts are blended with equal weight. With four or more sources the highest
and lowest are dropped first, so one outlier cannot drag the result.

Our own model is not one more equal voice. It is worse than the experts on
its own, but its disagreement with them points the right way, so the blend
is nudged a backtested share of the way toward it.
"""

import statistics


# Sources are trimmed only when there are at least this many.
MIN_SOURCES_TO_TRIM = 4

# The model is only trusted for players the experts expect to play a real
# role. The backtest grades only these, and the live blend and the value
# report use the same line.
MIN_RELEVANT_POINTS = 3.0


# The blended projection and the spread between sources (one standard
# deviation), from a {source: points} dict. Returns (0, 0) for no sources.
def blend(source_points):
    values = sorted(float(v) for v in source_points.values() if v is not None)
    if not values:
        return 0.0, 0.0

    spread = statistics.pstdev(values) if len(values) > 1 else 0.0
    if len(values) >= MIN_SOURCES_TO_TRIM:
        values = values[1:-1]
    return statistics.fmean(values), spread


# Move the experts' blend part of the way toward the model.
def nudge(expert, model, weight):
    return expert + weight * (model - expert)
