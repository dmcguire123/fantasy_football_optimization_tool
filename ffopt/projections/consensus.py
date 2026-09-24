"""
Blend several sources' projections into one number.

Twelve seasons of accuracy data from FantasyFootballAnalytics show a plain
average of sources beats nearly every single source, and that weighting
sources by past accuracy does not help, because which source is most
accurate changes from year to year. So the blend is an equal-weight
average. With four or more sources the highest and lowest are dropped
first, so one source's outlier cannot drag the result.

A source can be given weight zero to keep it on record but out of the
blend. Our own model starts that way, until a backtest shows it helps.
"""

import statistics


# Sources are trimmed only when there are at least this many.
MIN_SOURCES_TO_TRIM = 4

# Weight each source gets in the blend. A source not listed gets 1.
DEFAULT_WEIGHTS = {"model": 0.0}


# The blended projection and the spread between sources (one standard
# deviation), from a {source: points} dict. Returns (0, 0) for no sources.
def blend(source_points, weights=None):
    weights = DEFAULT_WEIGHTS if weights is None else weights
    entries = []
    for source, points in source_points.items():
        weight = weights.get(source, 1.0)
        if points is not None and weight > 0:
            entries.append((float(points), weight))
    if not entries:
        return 0.0, 0.0

    values = [points for points, _ in entries]
    spread = statistics.pstdev(values) if len(values) > 1 else 0.0

    entries.sort()
    if len(entries) >= MIN_SOURCES_TO_TRIM:
        entries = entries[1:-1]
    total_weight = sum(weight for _, weight in entries)
    mean = sum(points * weight for points, weight in entries) / total_weight
    return mean, spread
