"""
Walk-forward backtest: our model against ESPN, Sleeper, and the experts.

For every test season from 2018 on, the model is trained only on earlier
seasons and then predicts every game of the test season from features that
use only earlier games. Its predictions are graded next to:

  - ESPN's weekly projections and Sleeper's (Rotowire), both rescored with
    the league's rules;
  - their blend, which is what the app uses today;
  - simple baselines: season-to-date average, recent weighted average, and
    expected fantasy points alone;
  - FantasyPros weekly expert consensus ranks (2020 on), on rank order only,
    since ECR has no points.

Every method is graded on the same player-games: those both ESPN and Sleeper
projected, where the blend expected at least a few points, so the grade is
about fantasy-relevant decisions and not about who scores zero.

The promotion rule: the model earns a place in the live blend at a position
only if nudging the ESPN and Sleeper blend toward it makes the blend better
out of sample there, on both error and rank order. How far to nudge is
chosen each season from earlier seasons only.
"""

import json
import logging
import math
import time
from pathlib import Path

import polars as pl

from ..config import PROJECT_ROOT

from . import history
from .consensus import MIN_RELEVANT_POINTS, nudge
from .features import build_features, feature_columns


log = logging.getLogger(__name__)

POSITIONS = ("QB", "RB", "WR", "TE")
FIRST_TRAIN_SEASON = 2013


# ---------------------------------------------------------------- model


def make_model():
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=0.04,
        max_iter=400,
        max_leaf_nodes=15,
        min_samples_leaf=60,
        l2_regularization=1.0,
        random_state=7,
    )


# Train on every season before the test season, predict the test season.
# One model per position, since usage means different things at each.
def walk_forward(features, test_seasons):
    columns = feature_columns(features)
    predictions = []
    for season in test_seasons:
        for position in POSITIONS:
            train = features.filter(
                (pl.col("position") == position)
                & (pl.col("season") >= FIRST_TRAIN_SEASON)
                & (pl.col("season") < season)
                & (pl.col("career_games") > 0)
                & pl.col("pts").is_not_null()
            )
            test = features.filter((pl.col("position") == position) & (pl.col("season") == season))
            if train.is_empty() or test.is_empty():
                continue
            model = make_model()
            model.fit(train.select(columns).to_numpy(), train["pts"].to_numpy())
            predicted = model.predict(test.select(columns).to_numpy())
            predictions.append(
                test.select("player_id", "season", "week").with_columns(
                    pl.Series("model", predicted, dtype=pl.Float64)
                )
            )
        log.info("backtest: season %s done", season)
    return pl.concat(predictions)


# ------------------------------------------------------------- grading


def _spearman(a, b):
    ranked = pl.DataFrame({"a": a, "b": b}).with_columns(
        pl.col("a").rank(), pl.col("b").rank()
    )
    return ranked.select(pl.corr("a", "b")).item()


# MAE, RMSE, R-squared, and the average within-week rank correlation for a
# prediction column against actual points.
def grade(frame, column):
    errors = frame.select((pl.col(column) - pl.col("pts")).alias("e"))["e"]
    mae = errors.abs().mean()
    rmse = math.sqrt((errors**2).mean())
    variance = frame["pts"].var(ddof=0)
    r2 = 1 - (errors**2).mean() / variance if variance else float("nan")

    weekly = []
    for _, week in frame.group_by(["season", "week"]):
        if week.height >= 8:
            value = _spearman(week[column], week["pts"])
            if value is not None and not math.isnan(value):
                weekly.append(value)
    rank = sum(weekly) / len(weekly) if weekly else float("nan")
    return {"mae": mae, "rmse": rmse, "r2": r2, "spearman": rank, "n": frame.height}


# ------------------------------------------------------------ top level


def load_everything(scoring, seasons_for_stats, test_seasons):
    stats = history.load_player_stats(seasons_for_stats)
    opportunity = history.load_opportunity(seasons_for_stats)
    schedules = history.load_schedules()
    injuries = history.load_injuries(seasons_for_stats)
    features = build_features(stats, opportunity, schedules, scoring, injuries)

    ids = history.load_player_ids()
    espn = pl.concat(
        [
            history.score_projections(
                history.load_espn_projections(s), scoring, ids, "espn_id", "espn"
            )
            for s in test_seasons
        ]
    )
    sleeper = pl.concat(
        [
            history.score_projections(
                history.load_sleeper_projections(s), scoring, ids, "sleeper_id", "sleeper"
            )
            for s in test_seasons
        ]
    )
    ecr = history.load_weekly_ecr(schedules).join(
        history._id_frame(ids, "fantasypros_id").rename({"source_id": "fantasypros_id"}),
        on="fantasypros_id",
        how="inner",
    )
    ecr = ecr.select(
        "player_id", pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32), "ecr"
    ).unique(["player_id", "season", "week"])
    return features, espn, sleeper, ecr


# Candidate weights for the model inside the blend: the blend moves this
# share of the way from the experts' average toward the model.
WEIGHT_GRID = (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4)

RESULTS_PATH = PROJECT_ROOT / "data" / "projections" / "backtest.json"


# The weight with the lowest error on a set of games.
def best_weight(frame):
    best = (float("inf"), 0.0)
    for weight in WEIGHT_GRID:
        error = frame.select(
            (nudge(pl.col("espn_sleeper_blend"), pl.col("model"), weight) - pl.col("pts"))
            .abs()
            .mean()
        ).item()
        best = min(best, (error, weight))
    return best[1]


# The model's weight for each test season, chosen only from the seasons
# before it, so the graded blend never benefits from hindsight.
def walk_forward_weights(graded, test_seasons):
    rows = []
    for position in POSITIONS:
        at_position = graded.filter(pl.col("position") == position)
        for season in test_seasons:
            earlier = at_position.filter(pl.col("season") < season)
            weight = best_weight(earlier) if earlier.height else 0.0
            rows.append({"position": position, "season": season, "weight": weight})
    return pl.DataFrame(rows).with_columns(pl.col("season").cast(pl.Int32))


# How much of the model's disagreement with the experts shows up in the
# result, per position: the slope of (actual - experts) on (model - experts).
def edge_slopes(graded):
    slopes = {}
    for position in POSITIONS:
        frame = graded.filter(pl.col("position") == position).select(
            (pl.col("model") - pl.col("espn_sleeper_blend")).alias("gap"),
            (pl.col("pts") - pl.col("espn_sleeper_blend")).alias("beat"),
        )
        variance = frame["gap"].var()
        slopes[position] = (
            frame.select(pl.cov("gap", "beat")).item() / variance if variance else 0.0
        )
    return slopes


# Run the whole backtest. Returns the grades per position and method, the
# promotion decision and live weight per position, the edge slopes, and the
# graded games themselves.
def run(scoring, test_seasons=range(2018, 2026)):
    test_seasons = list(test_seasons)
    stats_seasons = range(history.FIRST_STATS_SEASON, max(test_seasons) + 1)
    features, espn, sleeper, ecr = load_everything(scoring, stats_seasons, test_seasons)

    predictions = walk_forward(features, test_seasons)

    def keyed(frame):
        return frame.with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32))

    graded = (
        features.filter(pl.col("season").is_in(test_seasons) & pl.col("pts").is_not_null())
        .select(
            "player_id", "position", "season", "week", "pts",
            "pts_season_avg", "prev_season_ppg", "pts_ewm_long", "xfp_ewm_long",
        )
        .join(predictions, on=["player_id", "season", "week"], how="inner")
        .join(keyed(espn), on=["player_id", "season", "week"], how="inner")
        .join(keyed(sleeper), on=["player_id", "season", "week"], how="inner")
        .join(ecr, on=["player_id", "season", "week"], how="left")
        .with_columns(
            ((pl.col("espn") + pl.col("sleeper")) / 2).alias("espn_sleeper_blend"),
            pl.coalesce("pts_season_avg", "prev_season_ppg", "pts_ewm_long").alias("season_avg"),
            pl.col("pts_ewm_long").alias("recent_avg"),
            pl.col("xfp_ewm_long").alias("expected_points"),
            (-pl.col("ecr")).alias("ecr_rank"),
        )
        .filter(pl.col("espn_sleeper_blend") >= MIN_RELEVANT_POINTS)
        .with_columns(
            pl.col("season_avg").fill_null(pl.col("espn_sleeper_blend")),
            pl.col("recent_avg").fill_null(pl.col("espn_sleeper_blend")),
            pl.col("expected_points").fill_null(pl.col("espn_sleeper_blend")),
        )
    )

    weights_by_season = walk_forward_weights(graded, test_seasons)
    graded = graded.join(weights_by_season, on=["position", "season"], how="left").with_columns(
        nudge(pl.col("espn_sleeper_blend"), pl.col("model"), pl.col("weight")).alias(
            "blend_with_model"
        )
    )

    methods = [
        "season_avg", "recent_avg", "expected_points",
        "espn", "sleeper", "espn_sleeper_blend", "model", "blend_with_model",
    ]
    rows = []
    for position in POSITIONS + ("ALL",):
        subset = graded if position == "ALL" else graded.filter(pl.col("position") == position)
        for method in methods:
            rows.append({"position": position, "method": method, **grade(subset, method)})

        # Rank order only, on the weeks and players ECR covers. ECR ranks
        # are within a position, so there is no ALL line for it.
        with_ecr = subset.filter(pl.col("ecr").is_not_null())
        if with_ecr.height and position != "ALL":
            for method in ("ecr_rank", "espn_sleeper_blend", "blend_with_model"):
                result = grade(with_ecr, method)
                rows.append(
                    {"position": position, "method": f"{method} (ECR weeks)",
                     "spearman": result["spearman"], "n": result["n"]}
                )

    table = pl.DataFrame(rows)
    promoted = promotion(table)
    weights = {
        position: best_weight(graded.filter(pl.col("position") == position))
        if promoted[position] else 0.0
        for position in POSITIONS
    }
    return {
        "table": table,
        "promoted": promoted,
        "weights": weights,
        "slopes": edge_slopes(graded),
        "graded": graded,
        "seasons": test_seasons,
    }


# The model joins the blend at a position only when the walk-forward
# weighted blend beats the experts alone on both error and rank order.
def promotion(table):
    decisions = {}
    for position in POSITIONS:
        rows = {r["method"]: r for r in table.filter(pl.col("position") == position).to_dicts()}
        before = rows.get("espn_sleeper_blend")
        after = rows.get("blend_with_model")
        if not before or not after:
            decisions[position] = False
            continue
        decisions[position] = (
            after["mae"] < before["mae"] and after["spearman"] > before["spearman"]
        )
    return decisions


# Save what the app needs from a run: the weights, slopes, and grades.
def save_results(result, path=RESULTS_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": time.strftime("%Y-%m-%d %H:%M"),
        "seasons": result["seasons"],
        "promoted": result["promoted"],
        "weights": result["weights"],
        "slopes": result["slopes"],
        "table": result["table"].to_dicts(),
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_results(path=RESULTS_PATH):
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None
