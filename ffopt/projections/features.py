"""
Model features, one row per player per game, built only from the past.

Each row describes a player going into a game: how much he has been used,
how productive that use has been, the game environment Vegas expects, and
how the opponent has defended his position. The target is what he actually
scored, in the league's scoring.

Every rolling feature is shifted by one game, so the row for week 7 only
sees weeks 1 to 6 (and earlier seasons). The Vegas lines are pre-game
numbers. That is what makes the backtest honest: at every week the model
knows exactly what it could have known at the time.

Opportunity is steadier than efficiency from week to week, so usage
(targets, carries, shares) and expected points get the most attention.
"""

import polars as pl

from .history import points_expression


# Per-game numbers we track, and the rolling views of each.
USAGE_COLUMNS = [
    "pts",
    "xfp",
    "pts_over_xfp",
    "targets",
    "receptions",
    "carries",
    "target_share",
    "carry_share",
    "air_yards_share",
    "wopr",
    "attempts",
    "passing_yards",
    "passing_tds",
    "rushing_yards",
    "receiving_yards",
    "rush_xfp",
    "rec_xfp",
    "pass_xfp",
]

# Half-lives, in games, for the exponentially weighted averages. A short one
# reacts to role changes; a long one knows the player's true level.
SHORT_HALF_LIFE = 2
LONG_HALF_LIFE = 6
DEFENSE_HALF_LIFE = 6
LEAGUE_HALF_LIFE = 17


# One row per player-game: actual league points plus usage, joined with
# expected fantasy points from ffopportunity.
def player_games(stats, opportunity, scoring):
    # Rows for a week not yet played have no result to score.
    if "upcoming" not in stats.columns:
        stats = stats.with_columns(pl.lit(False).alias("upcoming"))
    games = stats.with_columns(
        pl.when(pl.col("upcoming"))
        .then(None)
        .otherwise(points_expression(scoring))
        .alias("pts"),
        pl.col("season").cast(pl.Int32),
        pl.col("week").cast(pl.Int32),
    ).select(
        "player_id",
        pl.col("player_display_name").alias("name"),
        "position",
        "season",
        "week",
        "team",
        "opponent_team",
        "pts",
        *[
            pl.col(c).cast(pl.Float64)
            for c in [
                "targets",
                "receptions",
                "carries",
                "target_share",
                "air_yards_share",
                "wopr",
                "attempts",
                "passing_yards",
                "passing_tds",
                "rushing_yards",
                "receiving_yards",
            ]
        ],
    )

    expected = opportunity.select(
        pl.col("player_id"),
        pl.col("season").cast(pl.Int32),
        pl.col("week").cast(pl.Int32),
        pl.col("total_fantasy_points_exp").cast(pl.Float64).alias("xfp"),
        pl.col("rush_fantasy_points_exp").cast(pl.Float64).alias("rush_xfp"),
        pl.col("rec_fantasy_points_exp").cast(pl.Float64).alias("rec_xfp"),
        pl.col("pass_fantasy_points_exp").cast(pl.Float64).alias("pass_xfp"),
        pl.col("rush_attempt_team").cast(pl.Float64).alias("team_carries"),
    ).unique(["player_id", "season", "week"])

    games = games.join(expected, on=["player_id", "season", "week"], how="left")
    return games.with_columns(
        (pl.col("carries") / pl.col("team_carries")).alias("carry_share"),
        (pl.col("pts") - pl.col("xfp")).alias("pts_over_xfp"),
    ).sort(["player_id", "season", "week"])


# Rolling views of each usage column, using only earlier games.
def rolling_features(games):
    expressions = []
    for column in USAGE_COLUMNS:
        previous = pl.col(column).shift(1)
        expressions += [
            previous.ewm_mean(half_life=SHORT_HALF_LIFE, ignore_nulls=True)
            .over("player_id")
            .alias(f"{column}_ewm_short"),
            previous.ewm_mean(half_life=LONG_HALF_LIFE, ignore_nulls=True)
            .over("player_id")
            .alias(f"{column}_ewm_long"),
        ]
    expressions += [
        pl.col("pts").shift(1).over("player_id").alias("pts_last"),
        pl.col("pts").shift(1).cum_count().over("player_id").alias("career_games"),
        pl.col("pts").shift(1).cum_count().over(["player_id", "season"]).alias("season_games"),
        # Season-to-date average, before this game.
        (
            pl.col("pts").shift(1).cum_sum().over(["player_id", "season"])
            / pl.col("pts").shift(1).cum_count().over(["player_id", "season"])
        ).alias("pts_season_avg"),
        # Weeks since his last game: long gaps mean injury or a lost role.
        (pl.col("week") - pl.col("week").shift(1).over(["player_id", "season"])).alias(
            "weeks_since_last"
        ),
    ]
    games = games.with_columns(expressions)

    last_season = (
        games.group_by(["player_id", "season"])
        .agg(pl.col("pts").mean().alias("prev_season_ppg"), pl.len().alias("prev_season_games"))
        .with_columns((pl.col("season") + 1).alias("season"))
    )
    return games.join(last_season, on=["player_id", "season"], how="left")


# How many points each defense has allowed to each position, smoothed over
# its earlier games, attached to the offensive player facing it. The value
# going into a week is the smoothed total after the defense's last game
# before it, so it also works for a week not yet played.
def defense_features(games):
    allowed = (
        games.filter(pl.col("pts").is_not_null())
        .group_by(["opponent_team", "position", "season", "week"])
        .agg(pl.col("pts").sum().alias("allowed"))
    )
    # The typical points allowed to a position, from weeks up to and
    # including this one only, so the ratio never borrows from the future.
    league_average = (
        allowed.group_by(["position", "season", "week"])
        .agg(pl.col("allowed").mean().alias("week_average"))
        .sort(["position", "season", "week"])
        .with_columns(
            pl.col("week_average")
            .ewm_mean(half_life=LEAGUE_HALF_LIFE)
            .over("position")
            .alias("position_average")
        )
        .select("position", "season", "week", "position_average")
    )
    allowed = (
        allowed.join(league_average, on=["position", "season", "week"], how="left")
        .with_columns((pl.col("allowed") / pl.col("position_average")).alias("ratio"))
        .sort(["opponent_team", "position", "season", "week"])
        .with_columns(
            pl.col("ratio")
            .ewm_mean(half_life=DEFENSE_HALF_LIFE, ignore_nulls=True)
            .over(["opponent_team", "position"])
            .alias("opp_allowed_ratio"),
            (pl.col("season") * 100 + pl.col("week")).alias("when"),
        )
        .select("opponent_team", "position", "when", "opp_allowed_ratio")
        .sort("when")
    )
    games = (
        games.with_columns((pl.col("season") * 100 + pl.col("week")).alias("when"))
        .sort("when")
        .join_asof(
            allowed,
            on="when",
            by=["opponent_team", "position"],
            strategy="backward",
            allow_exact_matches=False,
            check_sortedness=False,
        )
        .drop("when")
    )
    return games.sort(["player_id", "season", "week"])


# Vegas expectations for the player's team in this game: implied points,
# spread, and whether it is at home. nflverse's spread_line is how many
# points the home team is favored by.
def game_features(games, schedules):
    lines = schedules.filter(pl.col("game_type") == "REG").select(
        pl.col("season").cast(pl.Int32),
        pl.col("week").cast(pl.Int32),
        "home_team",
        "away_team",
        pl.col("spread_line").cast(pl.Float64),
        pl.col("total_line").cast(pl.Float64),
    )
    home = lines.select(
        "season",
        "week",
        pl.col("home_team").alias("team"),
        (pl.col("total_line") / 2 + pl.col("spread_line") / 2).alias("implied_total"),
        pl.col("spread_line").alias("team_spread"),
        pl.lit(1).alias("is_home"),
    )
    away = lines.select(
        "season",
        "week",
        pl.col("away_team").alias("team"),
        (pl.col("total_line") / 2 - pl.col("spread_line") / 2).alias("implied_total"),
        (-pl.col("spread_line")).alias("team_spread"),
        pl.lit(0).alias("is_home"),
    )
    return games.join(pl.concat([home, away]), on=["season", "week", "team"], how="left")


# Game status as a number: how likely the report says he is to miss.
REPORT_STATUS = {"Questionable": 1, "Doubtful": 2, "Out": 3}
PRACTICE_STATUS = {
    "Limited Participation in Practice": 1,
    "Did Not Participate In Practice": 2,
}


# The week's injury report: the player's own status, and how much target
# and carry share his teammates who are ruled out leave behind. Vacated
# volume is where the experts are quickest and a usage average is slowest.
def injury_features(games, injuries):
    report = (
        injuries.filter(pl.col("game_type") == "REG")
        .select(
            pl.col("gsis_id").alias("player_id"),
            pl.col("season").cast(pl.Int32),
            pl.col("week").cast(pl.Int32),
            "team",
            pl.col("report_status").replace_strict(REPORT_STATUS, default=0).alias("report_status"),
            pl.col("practice_status")
            .replace_strict(PRACTICE_STATUS, default=0)
            .alias("practice_status"),
        )
        .unique(["player_id", "season", "week"])
    )
    games = games.join(
        report.select("player_id", "season", "week", "report_status", "practice_status"),
        on=["player_id", "season", "week"],
        how="left",
    ).with_columns(pl.col("report_status").fill_null(0), pl.col("practice_status").fill_null(0))

    # Each ruled-out player's usage going into the week he missed: the
    # latest of his rows from before that week.
    usage = games.select(
        "player_id",
        (pl.col("season") * 100 + pl.col("week")).alias("when"),
        pl.col("target_share_ewm_long").alias("share_targets"),
        pl.col("carry_share_ewm_long").alias("share_carries"),
    ).sort("when")
    out = (
        report.filter(pl.col("report_status") >= REPORT_STATUS["Doubtful"])
        .with_columns((pl.col("season") * 100 + pl.col("week")).alias("when"))
        .sort("when")
        .join_asof(
            usage,
            on="when",
            by="player_id",
            strategy="backward",
            allow_exact_matches=False,
            check_sortedness=False,
        )
    )
    vacated = out.group_by(["team", "season", "week"]).agg(
        pl.col("share_targets").fill_null(0).sum().alias("vacated_target_share"),
        pl.col("share_carries").fill_null(0).sum().alias("vacated_carry_share"),
    )
    return games.join(vacated, on=["team", "season", "week"], how="left").with_columns(
        pl.col("vacated_target_share").fill_null(0),
        pl.col("vacated_carry_share").fill_null(0),
    )


# Rows for a week not yet played: every player who has played this season
# (or last season, before week 2), on his latest team, facing that team's
# scheduled opponent. Their stats are empty; the features fill in from the
# games before.
def upcoming_rows(stats, schedules, season, week):
    recent = stats.filter(
        (pl.col("season") == season) | ((pl.col("season") == season - 1) & (week <= 2))
    ).filter((pl.col("season") < season) | (pl.col("week") < week))
    latest = (
        recent.sort(["season", "week"])
        .group_by("player_id")
        .agg(
            pl.col("player_display_name").last(),
            pl.col("position").last(),
            pl.col("team").last(),
        )
    )
    games = schedules.filter(
        (pl.col("season") == season) & (pl.col("week") == week) & (pl.col("game_type") == "REG")
    )
    opponents = pl.concat(
        [
            games.select(pl.col("home_team").alias("team"), pl.col("away_team").alias("opponent_team")),
            games.select(pl.col("away_team").alias("team"), pl.col("home_team").alias("opponent_team")),
        ]
    )
    return latest.join(opponents, on="team", how="inner").with_columns(
        pl.lit(season).cast(pl.Int32).alias("season"),
        pl.lit(week).cast(pl.Int32).alias("week"),
        pl.lit("REG").alias("season_type"),
        pl.lit(True).alias("upcoming"),
    )


# The full feature table. With upcoming=(season, week), rows for that
# not-yet-played week are added, with every feature but the result.
def build_features(stats, opportunity, schedules, scoring, injuries=None, upcoming=None):
    if upcoming:
        stats = pl.concat(
            [
                stats.with_columns(pl.col("season").cast(pl.Int32), pl.col("week").cast(pl.Int32)),
                upcoming_rows(stats, schedules, *upcoming),
            ],
            how="diagonal_relaxed",
        )
    games = player_games(stats, opportunity, scoring)
    games = rolling_features(games)
    games = defense_features(games)
    games = game_features(games, schedules)
    if injuries is not None:
        games = injury_features(games, injuries)
    return games


# Columns the model trains on: everything known before kickoff.
def feature_columns(frame):
    excluded = set(USAGE_COLUMNS) | {
        "player_id",
        "name",
        "position",
        "season",
        "team",
        "opponent_team",
        "team_carries",
    }
    return [c for c in frame.columns if c not in excluded]
