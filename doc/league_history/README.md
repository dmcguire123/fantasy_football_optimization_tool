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
    trades.json              every completed trade, both sides, players parsed (from emails)
    players.json             every player move from those trades, oldest first (from emails)
    draft.json               draft grade, projection, slot and summary next to the actual result (from emails)
  players_all_time.json      one entry per player with every trade they were part of
  source/
    gmail_trade_processed.json   the trade emails as copied (message ids only, no bodies)
    gmail_draft_recaps.json      the draft recap emails as copied
```

`trades.json`, `players.json` and `draft.json` come from Gmail, not the API, and exist only for years with matching emails.

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

## Where the email data came from

The NFL API is a dead end for anything beyond standings. NFL.com's "Trade Processed" emails, though, go to every manager in the league, so the mailbox holds a league-wide record of completed trades:

- **Trades: 39 completed trades, 2020 to 2025** (7, 3, 15, 8, 4 and 2 by year). One more trade email, dated 2020-11-03, was from a different league (`NFL-Managed 9105261`) and is excluded.
- **Draft recaps: 2021 to 2025**, for the mailbox owner's team (Put the Kittle On) only. There is no 2020 recap, only a "draft rescheduled" notice. A 2022 recap for a different league was excluded.

Each player string in the emails looks like `T. McLaurinWR - WASW. RobinsonWR - NYG`. `scripts/build_email_history.py` splits these and refuses to write anything that does not re-join to the exact original text. Team names are matched to team ids using each year's API `teams.json`; NFL.com cut long names with `...`, so those are matched by prefix. Defenses appear as `K. Chiefs` (DEF, KC).

Reading the trade files:

- The players listed under a team are the players **that team gave up**.
- `date` is when the "processed" email arrived, so it is the processing date, not the date the offer was made.
- Two 2022 emails (2022-10-03 and 2022-10-07) show the same Hunt-for-Akers swap between the same two teams. It may be a duplicate notice or a reversed and repeated trade; the emails cannot tell.

## What is not here (and why)

- **Week-by-week matchups and scores, full draft pick lists, league-wide waiver claims, rosters.** The NFL API's other endpoints (`league/matchups`, `schedule`, `transactions`, `settings`, `players`) answer `App Key is not authorized` for any key, and fantasy.nfl.com is offline. NFL.com's draft emails carry only a grade and a written summary, not the picks.
- **Rejected or cancelled trade offers, and the mailbox owner's own waiver claims.** The emails exist (proposals, rejections, cancellations, processed and unsuccessful claims) but only cover the mailbox owner's own team, so they were not turned into files. They are still in Gmail.
- **Seasons before 2020.** The league did not exist on NFL.com before then.

If someone has a valid NFL Fantasy `appKey` (for example from an old saved browser session or the retired mobile app's traffic), the key endpoints could be fetched into `raw/` with the same script. Nothing here guesses or uses keys that were not provided.

## Re-running

```
uv run python scripts/fetch_nfl_history.py --league 9072865 --from 2025 --to 2020
uv run python scripts/build_email_history.py
```

The first command overwrites the API-derived files with a fresh copy. The second rebuilds the trade, player and draft files from `source/`; run it after the first, since it reads each year's `teams.json`. The NFL API is undocumented and unofficial and could go offline at any time, which is why the raw responses are kept.
