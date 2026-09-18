# WBA Fantasy Classic: league history

History of the league from its NFL.com days (NFL.com league id `9072865`), before it moved to ESPN for 2026. ESPN only has 2026, so this folder is the only copy of 2020-2025 outside NFL.com's API.

## Where it came from

`api.fantasy.nfl.com/v2` still answers for two endpoints with no login: `league/standings` and `league/teams`. Both are fetched by `scripts/fetch_nfl_history.py`. The season is chosen with `gameId=10<year>` (for example `102024`). The `season`, `week` and `view` parameters are ignored, and `league/teams` ignores `gameId` and always returns the latest season.

Seasons that exist: **2020 to 2025** (2020 had 10 teams, 2021 onward 12). 2019 and earlier return no league.

## Layout

```
doc/league_history/
  all_time.json              champions by year, per-owner totals (titles, W-L-T, points)
  current_teams_raw.json     raw league/teams response (latest season only)
  <year>/
    raw/standings.json       raw API response, untouched
    league.json              name, size, draft type/date/status, divisions
    teams.json               team id, name, owner, co-manager, logo
    rankings.json            regular-season rank, record, streak, points for/against
    playoffs.json            seed, bracket (winners/consolation), final place, champion
    transactions_summary.json  add count, trade count, waiver priority per team
```

Numbers are converted from NFL's strings to real numbers in the derived files. Owners are matched across years by `owner_user_id`, so renaming a team does not split a person's record.

`rank` is the regular-season rank. `place` (in `playoffs.json`) is the final result after the playoffs; place 1 is the champion. They often differ.

## Champions

| Season | Team | Owner |
|---|---|---|
| 2020 | King Quon | Robert |
| 2021 | Zach Bottled It | Ryan |
| 2022 | Brooklyn Buckeens | Luke |
| 2023 | Brooklyn Buckeens | Luke |
| 2024 | Dublin Bay Buccaneers | Jonathan |
| 2025 | Dublin Bay Buccaneers | Jonathan |

## What is not here (and why)

The NFL API's other endpoints (`league/matchups`, `schedule`, `transactions`, `settings`, `players`) exist but answer `App Key is not authorized` for any key. Without an authorized key, and with fantasy.nfl.com offline, these are not available:

- week-by-week matchups and scores
- draft picks
- individual trades and waiver claims for the whole league
- rosters and player history

If someone has a valid NFL Fantasy `appKey` (for example from an old saved browser session or the retired mobile app's traffic), the key endpoints could be fetched into `raw/` with the same script. Nothing here guesses or uses keys that were not provided.

## Re-running

```
uv run python scripts/fetch_nfl_history.py --league 9072865 --from 2025 --to 2020
```

This overwrites the files above with a fresh copy. The API is undocumented and unofficial and could go offline at any time, which is why the raw responses are kept.
