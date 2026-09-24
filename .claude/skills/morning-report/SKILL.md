---
name: morning-report
description: Build today's fantasy football morning report for this league and email it with Gmail. Use when asked for the morning report, the daily fantasy email, or when invoked as /morning-report <recipient email>.
argument-hint: <recipient email>
allowed-tools: Bash(.venv/bin/python -m ffopt report*), mcp__claude_ai_Gmail__send_message
---

# Morning report

Send the owner of this fantasy team a short, useful morning email about their
ESPN league. The recipient is `$ARGUMENTS`. If no address was given, stop and
say so. Don't guess one.

## 1. Get the data

Run this from the repository root:

```
.venv/bin/python -m ffopt report
```

It prints one JSON document and changes nothing in the league. It contains:
- `lineup`: moves needed, win probability as set and if optimized, starters with per-source projections
- `matchup`: opponent, projected scores, margin, their questionable starters
- `roster_alerts`: players on the roster who are injured or on bye, and whether they start
- `waivers.targets`: the best pickups, with weekly and season gain, who to drop, and the FAAB bid
- `value`:
  - `model_pickups`: expected edge over the experts, already sized by the backtest
  - `espn_behind`: players the other sources rate well above ESPN
  - `roster_warnings` and `roster_boosts`
- `trends` (may be null):
  - `my_weekly_moves`: your players whose projection this week moved 3+ points against their earlier weeks
  - `my_ros_moves`: your players whose rest-of-season projection moved over the last week
  - `available_ros_risers`: free agents whose rest-of-season projection is climbing

If the command fails, send a three-line email saying the report failed, with
the last line of the error. Then stop.

## 2. Decide what matters today

Lead with the one or two things to do today. Order by urgency:
1. A starter who is out, doubtful, or on bye, or lineup moves that raise win probability. These come first on every day of the week.
2. Waiver claims on Monday and Tuesday. Tuesday is the last morning before waivers run.
3. Start/sit calls on Thursday (the Thursday game) and on Sunday.
4. Value picks: `model_pickups` with expected edge of 0.5 or more, and `espn_behind`. Your leaguemates see ESPN's numbers, so an `espn_behind` player is the easiest to steal.
5. Trends: one line on the biggest mover on the roster (up or down), plus any free agent in `available_ros_risers`. A rising free agent is a pickup to watch even if he isn't a waiver target yet.

Days with nothing to act on still get an email, but keep it very short: the record, the matchup outlook, and "nothing to do today".

Rules:
- Use only numbers from the JSON. Don't invent projections, news, or reasons.
- Call the blended number "projection". Mention a single source only when the sources disagree by 3 or more points.
- "Edge" means expected points above the experts' consensus. Keep it to one decimal.
- Never tell the reader a move was made. This report only recommends. Moves go through `python -m ffopt apply-lineup` or `claim`, or the web app.

## 3. Write and send the email

Subject: `Fantasy AM: <short headline>`, for example `Fantasy AM: bench Dobbins, 59% vs Zach`.

Send it with `mcp__claude_ai_Gmail__send_message`:
- `to`: the recipient
- `htmlBody`: simple HTML, described below
- `body`: a plain-text version of the same content, with no Markdown

The HTML:
- a one-line headline
- a "Do today" list, with at most 3 items
- a small matchup line: you vs them, win %, projected margin
- up to 3 waiver or value targets, each with its bid or edge
- roster alerts, if any
- a "Trending" line or two, if `trends` has anything

Use inline styles only, one font, and no images. Keep it under 250 words.

After sending, reply with one line: the subject and who it went to.
