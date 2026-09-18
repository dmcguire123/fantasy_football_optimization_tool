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

Then open http://127.0.0.1:8000. Four tabs:

- **My Team** — your roster, the recommended lineup, and a button that applies
  the changes after showing you exactly what it will submit.
- **Waiver Wire** — ranked targets with a suggested drop and bid on each row,
  and a claim button per player.
- **Scout Opponent** — this week's matchup, broken down by position.
- **League** — power rankings, any team's roster on demand, and trade targets.

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

- Projections are ESPN's own. The optimizer is only as good as they are, and
  it will happily start someone ESPN is too optimistic about. Treat the
  recommendations as a strong prior, not an oracle.
- ESPN's fantasy API is undocumented and unversioned. It changes without
  notice, usually around the start of a season.
- Players whose games have already kicked off are locked by ESPN. It will
  reject moves involving them, which is the correct outcome.
- Win probability uses a normal approximation around the projected scores. It
  is a rough guide, not a simulation.
- League-wide analysis reads every roster in one request, which ESPN allows,
  but the app caches results briefly rather than refetching per widget.
