"""
Our own weekly projection model, for the week about to be played.

The same features and the same model settings the backtest graded
(features.py, backtest.make_model), trained on every finished game since
2013 and applied to the players with a game this week. One model per
position.

On its own the model is less accurate than the experts, so it never
replaces them. Its value is where it disagrees with them: the backtest
found that when the model sits well above the experts, players beat the
experts' number on average, by about a fifth of the gap. The service nudges the blend toward
the model by the weight the backtest chose, and the value report surfaces
the biggest disagreements.
"""

import logging

import polars as pl

from . import history
from .backtest import FIRST_TRAIN_SEASON, POSITIONS, make_model
from .features import build_features, feature_columns


log = logging.getLogger(__name__)


# Projected league points for every player with a game in the given week,
# keyed by nflverse (gsis) id.
def predict_week(season, week, scoring):
    seasons = range(history.FIRST_STATS_SEASON, season + 1)
    features = build_features(
        history.load_player_stats(seasons),
        history.load_opportunity(seasons),
        history.load_schedules(),
        scoring,
        history.load_injuries(seasons),
        upcoming=(season, week),
    )
    columns = feature_columns(features)
    is_past = (pl.col("season") < season) | (
        (pl.col("season") == season) & (pl.col("week") < week)
    )

    predictions = []
    for position in POSITIONS:
        train = features.filter(
            (pl.col("position") == position)
            & (pl.col("season") >= FIRST_TRAIN_SEASON)
            & pl.col("pts").is_not_null()
            & (pl.col("career_games") > 0)
            & is_past
        )
        upcoming = features.filter(
            (pl.col("position") == position)
            & (pl.col("season") == season)
            & (pl.col("week") == week)
            & pl.col("pts").is_null()
        )
        if train.is_empty() or upcoming.is_empty():
            continue
        model = make_model()
        model.fit(train.select(columns).to_numpy(), train["pts"].to_numpy())
        predictions.append(
            upcoming.select("player_id", "name", "position", "team").with_columns(
                pl.Series("model", model.predict(upcoming.select(columns).to_numpy()))
            )
        )
    if not predictions:
        return pl.DataFrame()
    return pl.concat(predictions)
