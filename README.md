# Fantasy Football Optimization Tool

Connects to an ESPN fantasy football league, works out the best lineup you can
start each week, ranks the waiver wire by what each player would actually add
to that lineup, scouts every other team in the league, and submits your roster
moves and waiver claims back to ESPN.

There is a browser app and a command line tool. Both sit on the same Python
package, so anything one can do, the other can too.

## What it does

**Lineup.** Solves the assignment problem exactly: every starting slot gets an
eligible player, nobody is used twice, and the projected total is the highest
available. Greedy "best player at each position" ordering gets FLEX wrong;
this does not. Players on a bye or ruled out are treated as zero, and injured
reserve is left alone. It then reports the exact slot swaps needed, skipping
moves that would shuffle interchangeable players without changing anything.

**Waivers.** Every available player is scored by re-solving your optimal lineup
with that player added and one of yours dropped. The difference is what the
pickup is worth. A great receiver is worth little if you already start three
better ones; a mediocre tight end is worth a lot if yours is on a bye. Each
recommendation comes with the cheapest sensible player to drop and a suggested
FAAB bid sized against your remaining budget.

**Scouting.** Power rankings blend season scoring with current roster strength.
The opponent report gives both teams' ceilings, a win probability, a
position-by-position breakdown, and a list of their starters who may not play.
Trade targets scan every other roster for players who would improve your
lineup, flag the ones their team can spare, and suggest what to send back.

**Win probability.** Each player's week-to-week swing is measured from real
nflverse game logs (this season and last, downloaded once and cached under
`data/`), so the win-probability lineup objective knows who is boom-or-bust.
Set `FFOPT_NFLVERSE=false` to use fixed per-position spreads instead. If the
download fails it falls back to those spreads automatically.

**Projections from more than one source.** ESPN is one opinion. By default
(`FFOPT_PROJECTIONS=consensus`) every projection in the app is a blend of
ESPN, Sleeper (Rotowire), and FantasyPros (with a free API key), each one
rescored with your league's exact scoring rules, plus a small nudge from our
own model where it has earned one. Rest-of-season numbers are real: the sum of
each source's remaining weekly projections, not ESPN's preseason total. See
[Projections](#projections) below.

**History and calibration.** `python -m ffopt snapshot` records the model's
win probability for every matchup in the league, and settles finished games
with their real scores. Run it once a week (or `--backfill` to rebuild earlier
weeks). `python -m ffopt calibration` then shows whether a 70% call really
wins about 70% of the time, and whether the measured player spreads beat the
old fixed spread. Records live in `data/history.db`.

## Setup

### 1. Install

```bash
uv venv
uv pip install -r requirements.txt
```

Run commands below with `uv run --no-project python -m ffopt ...`, or activate the
environment once with `source .venv/bin/activate` and use `python -m ffopt ...`.

### 2. Tell it about your league

```bash
cp .env.example .env
```

Then open `.env` and fill in the values. It needs four things.

| Setting | Where to find it |
| --- | --- |
| `ESPN_LEAGUE_ID` | The `leagueId` in your league's URL on fantasy.espn.com |
| `ESPN_SEASON` | The year the season started, so 2025 for the 2025-26 season |
| `ESPN_TEAM_ID` | The `teamId` in the URL when your own team is open |
| `ESPN_SWID` and `ESPN_S2` | Two browser cookies, see below |

Private leagues need the two cookies, and so does every roster move even in a
public league. To get them, log in to fantasy.espn.com, open developer tools
with F12, find the cookie list for `espn.com`, and copy the values of `SWID`
and `espn_s2`.

- Chrome or Edge: Application, then Cookies, then `https://fantasy.espn.com`
- Firefox: Storage, then Cookies
- Safari: enable the Develop menu first, then Web Inspector, then Storage

`SWID` includes its curly braces. `espn_s2` is long and contains percent signs;
paste the whole thing. These cookies are your login. `.env` is git-ignored, and
the app never sends them anywhere except ESPN.

They expire every so often. When they do, requests start failing with a message
saying the cookie has expired, and you repeat this step.

### 3. Check the connection

```bash
python -m ffopt info
```

That prints the league name, the current week, your starting lineup shape, and
your team's record. If your team is wrong, fix `ESPN_TEAM_ID`.

## The browser app

```bash
python -m ffopt serve
```

Then open http://127.0.0.1:8000. The tabs:

- **My Team**: your roster, the recommended lineup, and a button that applies
  the changes after showing you exactly what it will submit.
- **Season**: your whole season week by week. It shows each opponent, final
  scores for weeks played, and both teams' projected totals and your win
  probability for every week to come, with a chart. Pick any week (say week
  8) to see both teams' best lineups side by side, with each player's NFL
  game, bye, and injury status. You can switch to any other team's season.
- **Players**: search any rostered player or top-300 free agent. You get this
  week, next week, and every remaining week by source (ESPN, Sleeper,
  FantasyPros, our model), the blended projection, his NFL opponent and bye,
  past weeks against what he actually scored, and rest of season by source.
- **Waiver Wire**: ranked targets with a suggested drop and bid on each row,
  and a claim button per player.
- **Value**: pickups our model likes, and players the other sources rate
  above ESPN.
- **News**: openings from breaking news, with an Add button that asks first.
- **Trends**: projection risers and fallers, and each player's history.
- **Scout Opponent**: this week's matchup, broken down by position.
- **League**: power rankings, any team's roster on demand, and trade targets.

Future weeks use the average of ESPN's and Sleeper's projections for that
week, with byes applied. FantasyPros only projects the current week, and our
model's future weeks are shown but not blended. Future matchups use today's
rosters, so they show what happens if nobody makes a move.

## Waiver wire intelligence

The Waiver Wire tab has four sub-tabs:

- **The List**: waiver picks gathered from the web (Sleeper trending adds, plus
  waiver articles from FantasyPros, RotoBaller, CBS, Yahoo, 4for4, PFF and
  others), merged into one ranked list. Names are matched against players who
  are actually available in your league. The Athletic is paywalled, so only its
  public headlines and summaries are read. A source that is down is shown as
  such and never blocks the rest.
- **Current Rosters**: every team's roster, what it is thin at, and its FAAB.
  A team thin at a position is a likely rival bidder there.
- **My Targets**: candidates ranked for your team, with the best drop, the
  lineup gain, and a bid as a percent of the FAAB you have left. Kickers and
  defenses are capped at small bids.

- **Stash Picks**: players worth holding as insurance even if they never
  start. Scored in expected points over the rest of the season from injury
  cover for your starters (with handcuffs assumed to inherit 65% of the
  starter's output), bye-week cover, and points rivals would gain by adding
  the player. Bids are capped at 8% of remaining FAAB.

Every projection comes from ESPN under your league's own scoring, so gains,
stash values and bids already reflect PPR. Only the web consensus is
scoring-agnostic, and it just decides who gets a look.

Set `ANTHROPIC_API_KEY` in `.env` and Claude writes the priority and reasoning
for each target. Without a key a plain heuristic does it. Claude only writes
text: it never sets a bid or submits a claim, and its reply is validated first.
Scraped pages are fetched once and reused for `FFOPT_INTEL_TTL` seconds.

Endpoints: `GET /api/waivers/consensus`, `/api/waivers/rosters`,
`/api/waivers/targets`, `/api/waivers/stash`.

## Projections

### Where the numbers come from

| Source | How | Notes |
|---|---|---|
| ESPN | your league's feed, plus ESPN's public feed for future weeks | already in your scoring |
| Sleeper | public API, no key | Rotowire's projections |
| FantasyPros | official API, `FANTASYPROS_API_KEY` in `.env` | expert average; [free personal key](https://secure.fantasypros.com/api-keys/request/), limited (see below) |
| Our model | trained on nflverse data, 2013 on | only nudges the blend; see below |

Sleeper and FantasyPros give stat lines, which are scored with your league's
`scoringSettings`. That scoring engine reproduces ESPN's own league-scored
projections exactly, at every position including K and D/ST. The experts are
averaged with equal weight. Twelve seasons of public accuracy research, and
our own backtest, both find that a plain average beats any single source, and
that weighting sources by their past accuracy doesn't help.

The free FantasyPros key allows 1 call a second and 100 calls a day, with
at most 10 players per call, for personal, non-commercial use. So the app
asks for players by id, 10 at a time. Your roster goes first, then everyone
else's rostered players, then the 60 most-owned free agents. Each player is
fetched at most once a day, and the app stops at 90 calls
(`data/projections/fantasypros_calls.json`). Players it didn't get to just
use ESPN and Sleeper until the next day.

Every pull is saved to `data/projections.db`, so each source can be graded on
this season later. Nobody else keeps old weekly projections around.

### The backtest

```bash
python -m ffopt backtest           # 2018-2025, scored with your league's rules
```

This grades ESPN, Sleeper, their blend, simple baselines, and our model on
every fantasy-relevant player-game from 2018 to 2025. The model is always
trained only on earlier seasons, and its features only see earlier games. It
also checks rank order against FantasyPros' weekly expert rankings (2020 on).
The results as of September 2026, in full PPR:

| | MAE | Weekly rank correlation |
|---|---|---|
| Season-to-date average | 5.70 | 0.502 |
| Expected fantasy points (nflverse) | 5.50 | 0.526 |
| ESPN | 5.30 | 0.577 |
| Sleeper | 5.29 | 0.578 |
| **ESPN + Sleeper blend** | **5.24** | **0.587** |
| Our model alone | 5.37 | 0.548 |
| **Blend nudged toward our model** | **5.23** | **0.587** |

What this means:

- The blend beats every single source at every position. That is why the app
  uses it.
- Our model alone beats the simple baselines, but not the experts. They know
  things public data doesn't: news, depth charts, coaches' plans.
- Where the model disagrees with the experts, its disagreement points the
  right way in each of the eight seasons. About 20% of the gap shows up in
  the result.
  Nudging the blend toward the model helped at RB and WR, so the model gets a
  0.25 weight there. At QB and TE it didn't help on both error and rank order,
  so it gets none.

The weights and edge sizes are saved to `data/projections/backtest.json`, and
the app reads them from there. Rerun the backtest after changing the model.

### Undervalued players

```bash
python -m ffopt value              # or the Value tab in the browser app
```

- **Pickups our model likes.** Available players the model rates above the
  experts, shown as expected points of edge, sized by the backtest.
- **Pickups the other sources rate above ESPN.** Your leaguemates see ESPN's
  numbers, so these are the players they are most likely to overlook. ESPN
  often leaves a backup at 0.0 for a week after he takes over a starting job.
- **Your players the model is worried about**, and the ones it likes.

### Trends

```bash
python -m ffopt trends                     # risers and fallers
python -m ffopt trends --player "Wan'Dale" # one player, week by week and by source
```

The **Trends** tab in the browser app shows the same data, with a chart of
each player's projections against what he actually scored.

- **Weekly:** each week's projection from ESPN, Sleeper, FantasyPros, our
  model, and the blend, next to the actual score. ESPN's and Sleeper's past
  weeks come from their own feeds, so this covers the whole season. The
  others begin when the app started archiving. Weekly movers are players
  whose projection this week sits 3+ points off their earlier weeks: a new
  role, a return from injury, a benching.
- **Rest of season:** each source's ROS total, one point per day it was
  pulled. Nobody publishes old ROS numbers, so this history starts with the
  app's first archived pull and grows every day the morning email or the
  web app runs. ROS movers compare today to a pull at least five days old.

Our model now projects the rest of the season too. It takes each player's
current features and pairs them with every remaining game: opponent, home
or away, and the Vegas line where one is posted, or the team's average so
far where it isn't. It's shown next to the other sources but kept out of
the ROS blend, because only its weekly numbers have been backtested.

### Running it every week

```bash
python -m ffopt projections        # pull, archive, and compare every source
scripts/weekly_projections.sh      # projections + value report into data/logs/
```

To run it automatically on Tuesday morning, Thursday afternoon, and Sunday
morning, install the LaunchAgent:

```bash
sed "s#__REPO__#$PWD#" scripts/launchd/com.ffopt.projections.plist \
  > ~/Library/LaunchAgents/com.ffopt.projections.plist
launchctl load ~/Library/LaunchAgents/com.ffopt.projections.plist
```

The model needs the packages in `requirements.txt` (nflreadpy, polars,
scikit-learn). Set `FFOPT_MODEL=false` to skip it, or `FFOPT_PROJECTIONS=espn`
to go back to ESPN's numbers alone.

## Morning email

Every morning at 7:30 you can get a short email: what to do today (lineup
fixes, injured or bye-week starters, waiver claims on Monday and Tuesday),
your matchup and win probability, the best pickups and value plays, roster
alerts, and the biggest projection movers.

How it works:
1. `python -m ffopt report` gathers everything as JSON. It only reads.
2. The project skill in `.claude/skills/morning-report/` has Claude pick
   what matters today and write the email.
3. Claude sends it through its Gmail connector.

Setup:
1. Set `FFOPT_REPORT_EMAIL` in `.env`. Set `CLAUDE_CONFIG_DIR` too if you
   use a non-default Claude config directory.
2. Make sure `claude mcp list` shows Gmail as connected.
3. Send one now: `scripts/morning_report.sh`. It logs to
   `data/logs/morning_report.log`. From inside Claude Code, run
   `/morning-report you@example.com` instead.
4. Schedule it:

```bash
sed "s#__REPO__#$PWD#" scripts/launchd/com.ffopt.morning-report.plist \
  > ~/Library/LaunchAgents/com.ffopt.morning-report.plist
launchctl load ~/Library/LaunchAgents/com.ffopt.morning-report.plist
```

To stop it, run `launchctl unload ~/Library/LaunchAgents/com.ffopt.morning-report.plist`.
The Mac has to be awake (or wake) at 7:30. launchd runs a missed job when
the Mac next wakes up.

## Breaking news

The news watcher looks for pickups the moment a story breaks, before the
rest of the league reacts. Every 3 minutes it checks:

- **ESPN/Rotowire player news** for the 600 most-owned players. One request
  shows whose news changed, and only those players' new items are fetched.
- **ESPN injury statuses**, for a starter who turns Out, Doubtful, or IR.
- **Sleeper add rushes**: a player added at twice his normal hourly pace or
  more.

A starter ruled out, placed on IR, benched, or released opens a spot for
his backups. A player named the starter is an opening for himself. Each
opening that lands on a free agent (or a waiver player) in your league is
sized like any waiver pickup. The projections won't have caught up yet, so
the backup is credited with a share of the starter's usual week, for the
weeks he is expected to miss. You get alerted (a Mac notification plus one
email per scan) when an opening:

- improves your lineup (1+ point this week, or 6+ rest of season), or
- is a starter-level player at his position for anyone, worth grabbing
  before a rival does.

The strongest openings (3+ points this week, or 15+ rest of season, not a
one-week stream, and never dropping one of your starters) come as a
**suggested claim**. It spells out the add, the drop, and the bid, with the
command to run it. **The watcher never makes a roster move.** You make it
from the email's command, the **News** tab's Add button (which asks first),
or `python -m ffopt claim`.

```bash
python -m ffopt news scan      # one scan now (the first one records a baseline)
python -m ffopt news list      # openings worth an alert; --all for everything
```

To run it every 3 minutes (it runs read-only; each quiet scan takes about a
second):

```bash
sed "s#__REPO__#$PWD#" scripts/launchd/com.ffopt.news.plist \
  > ~/Library/LaunchAgents/com.ffopt.news.plist
launchctl load ~/Library/LaunchAgents/com.ffopt.news.plist
```

Alerts are emailed to `FFOPT_REPORT_EMAIL`. Set `FFOPT_NEWS_EMAIL=false` for
Mac notifications only. Scans are logged to `data/logs/news.log`.

## The command line

```bash
python -m ffopt info                 # league and team summary
python -m ffopt roster               # your roster, starters first
python -m ffopt roster --team-id 4   # somebody else's roster
python -m ffopt lineup               # optimal lineup and the moves to get there
python -m ffopt apply-lineup         # submit those moves, after confirming
python -m ffopt waivers              # ranked waiver targets with bids
python -m ffopt claim --add 4262921 --drop 3139477 --bid 14
python -m ffopt scout                # this week's opponent
python -m ffopt teams                # power rankings
python -m ffopt trades               # trade targets and what to offer
python -m ffopt projections          # every projection source, side by side
python -m ffopt value                # undervalued players this week
python -m ffopt backtest             # grade the sources and our model on 2018-2025
python -m ffopt report               # today's report as JSON (feeds the morning email)
python -m ffopt trends               # projection risers and fallers; --player NAME for one
python -m ffopt news scan            # breaking news: openings on players free in your league
python -m ffopt serve                # run the browser app
```

Every command takes `--week N` to look at a different week. Commands that
change your roster ask for confirmation; `--yes` skips the prompt.

## Not submitting things by accident

Three separate guards, because a roster move is hard to take back.

1. Every API write defaults to a dry run. It returns the exact payload it
   would have sent and posts nothing. Submitting takes `dry_run: false`.
2. The browser app shows the specific moves and waits for you to confirm.
3. The CLI prompts before anything reaches ESPN.

On top of that, `FFOPT_READ_ONLY=true` in `.env` blocks all writes outright,
which is a good way to run it for a week while you decide whether you trust
the recommendations.

## HTTP API

Served alongside the UI. Interactive docs are at `/docs`.

| Method and path | What it does |
| --- | --- |
| `GET /api/config` | Whether the league is configured, with cookies redacted |
| `GET /api/league` | Settings, week, and all teams |
| `GET /api/teams/{id}` | One roster, with how efficiently it is set |
| `GET /api/my-team` | Your roster plus the optimal lineup |
| `GET /api/lineup/optimize` | Optimal lineup, `horizon=week` or `season` |
| `POST /api/lineup/apply` | Submit lineup changes |
| `GET /api/free-agents` | The available player pool |
| `GET /api/waivers/recommendations` | Ranked pickups, drops, and bids |
| `POST /api/waivers/claim` | Submit a waiver claim or free agent add |
| `GET /api/transactions/pending` | Claims you have already submitted |
| `GET /api/scouting/power-rankings` | The league ranked |
| `GET /api/scouting/opponent` | This week's matchup report |
| `GET /api/scouting/trade-targets` | Who to trade with and for what |
| `GET /api/scouting/positional-surplus` | Roster depth by position, league-wide |

## Tests

```bash
uv run --no-project python -m pytest
```

The whole suite runs against a synthetic league served by a mock HTTP
transport, so no test touches the network or your real team. The optimizer is
additionally checked against brute force over randomized rosters.

## Worth knowing

- Projections are a blend of several sources (see [Projections](#projections)).
  The optimizer is only as good as they are. Treat the recommendations as a
  strong prior, not an oracle.
- ESPN's past weekly projections come from its public feed. There is no way
  to confirm they are exactly what ESPN showed before kickoff. They grade
  about the same as Sleeper's, which doesn't suggest hindsight, but the
  archive in `data/projections.db` is the clean record from now on.
- ESPN's fantasy API is undocumented and unversioned. It changes without
  notice, usually around the start of a season.
- Players whose games have already kicked off are locked by ESPN. It will
  reject moves involving them, which is the correct outcome.
- Win probability uses a normal approximation around the projected scores. It
  is a rough guide, not a simulation.
- League-wide analysis reads every roster in one request, which ESPN allows,
  but the app caches results briefly rather than refetching per widget.
