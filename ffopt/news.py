"""
Breaking news watcher: spot the pickup before the rest of the league does.

Each scan is cheap enough to run every few minutes:

  1. One ESPN request returns the 600 most-owned players, each with the time
     of his latest news item, his injury status, and whether he is a free
     agent, on waivers, or on a team in this league.
  2. For the few players whose news time moved since the last scan, fetch
     the new items (Rotowire's blurbs, through ESPN).
  3. Sleeper's adds over the last hour show players other managers are
     suddenly rushing to pick up.

Each new item is classified. A starter ruled out, placed on injured reserve,
benched, or released is an opening for his backups. A player named the
starter or taking over a role is an opening for himself. An opening that
lands on a player who is free (or on waivers) in this league is sized the
way the waiver wire sizes any pickup: re-solve your lineup with him, this
week and over the rest of the season. Because the projections have not
caught up with the news yet, a backup is credited with a share of the
injured starter's typical week for the weeks he is expected to miss.

Worthwhile openings are sent as an alert (a Mac notification, and an email
when set up). The strongest ones come with a suggested claim, spelled out
(add, drop, bid) with the command to make it. The watcher never makes a
roster move itself: you decide, from the email, the News tab, or the CLI.

State lives in data/news.db: what each player looked like at the last scan,
which news items have been seen, and every opening found.
"""

import calendar
import json
from html import escape as html_escape
import logging
import re
import sqlite3
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import httpx

from . import constants as C
from . import waivers
from .config import PROJECT_ROOT
from .models import player_from_raw
from .optimizer import season_projection, solve_lineup, week_projection


log = logging.getLogger(__name__)

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "news.db"
NEWS_URL = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players"
TRENDING_URL = "https://api.sleeper.app/v1/players/nfl/trending/add"

WATCH_LIMIT = 600
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# A player's absence matters only if he was fantasy relevant.
MIN_RELEVANT_OWNED = 25.0
MIN_RELEVANT_PROJECTION = 6.0

# Share of an injured starter's typical week his backup is credited with.
INHERIT_SHARE = {"QB": 0.85, "RB": 0.6, "WR": 0.35, "TE": 0.45}
# Backups considered per opening.
MAX_BENEFICIARIES = 2

# A rush: a player's Sleeper adds in the last hour, against his average
# hour over the last six. Big names are added all day, so the jump is what
# counts, with a floor so a few adds of an obscure player don't.
TRENDING_WINDOW_HOURS = 1
TRENDING_BASELINE_HOURS = 6
TRENDING_MIN_ADDS = 2000
TRENDING_MIN_SPIKE = 2.0

# An opening is worth an alert at this much gain to your lineup, or when the
# player would be a starter-level play at his position for anyone (worth
# grabbing before a rival does, to stash, block, or trade).
ALERT_MIN_WEEKLY = 1.0
ALERT_MIN_SEASON = 6.0
STARTER_LEVEL = {"QB": 16.0, "RB": 10.0, "WR": 10.0, "TE": 8.0}

# A suggested claim: a clearly strong gain for your lineup, not a one-week
# stream, and a drop who isn't in this week's best lineup.
SUGGEST_MIN_WEEKLY = 3.0
SUGGEST_MIN_SEASON = 15.0

UNAVAILABLE_STATUSES = {"OUT", "INJURY_RESERVE", "SUSPENSION", "DOUBTFUL"}

# Story wording that takes a player out of the lineup, and how long for.
# Checked in order; the first match wins.
OUT_PATTERNS = [
    (r"season-ending|out for the (rest of the )?season|tore (his )?acl|torn acl|achilles", "season"),
    (r"placed on (injured reserve|ir)\b|to (injured reserve|ir)\b|\bon (injured reserve|ir)\b", "ir"),
    (r"\breleased\b|\bwaived\b|\bcut by\b", "season"),
    (r"\bbenched\b|\bdemoted\b|lost (his|the) starting|no longer (the )?start", "season"),
    (r"\bsuspended\b", "multi"),
    (r"out (at least |for )?(several|multiple|a few) weeks|out indefinitely|expected to miss (several|multiple)|"
     r"multi-week|weeks to recover", "multi"),
    (r"ruled out|won't play|will not play|will miss|expected to miss|\bout for\b|inactive for|"
     r"won't suit up|not expected to play", "week"),
]
ROLE_PATTERNS = [
    r"named (the )?(starter|starting)", r"will start\b", r"expected to start", r"set to start",
    r"in line (for|to) (start|a larger|more|the bulk)", r"take over", r"lead back",
    r"(starting|feature|workhorse|bell-?cow) role", r"first-team reps",
    r"see (a )?(bigger|larger|increased) role",
]


# ------------------------------------------------------------ classify


# What a news item means: ("out", how long) for a player losing his spot,
# ("role", None) for a player gaining one, or (None, None). Only the
# headline is read, and with a player given it must be about him. Headlines
# often name teammates: in "Mariota will start with Daniels ruled out" the
# absence is Daniels's, so an "out" phrase that comes after another
# player's surname (other_names) is not held against this player.
def classify(text, player=None, other_names=()):
    text = (text or "").lower()
    if player is not None and not mentions(player, text):
        return None, None
    own = surname(player) if player is not None else None
    own_at = text.find(own) if own else 0
    for pattern, length in OUT_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        between = text[own_at:match.start()]
        if any(re.search(rf"\b{re.escape(name)}\b", between)
               for name in other_names if name and name != own):
            continue
        return "out", length
    for pattern in ROLE_PATTERNS:
        if re.search(pattern, text):
            return "role", None
    return None, None


# A player's last name, lowercased, without a suffix like "Jr.".
def surname(player):
    words = [w for w in re.sub(r"[^a-z' -]", "", player.name.lower()).split()
             if w not in ("jr", "sr", "ii", "iii", "iv")]
    return words[-1] if words else ""


# Whether a headline is about this player: Rotowire's start with his last
# name.
def mentions(player, text):
    name = surname(player)
    return bool(name) and name in text.lower()


# Weeks a player is expected to miss, by how the story described it.
def missed_weeks(length, weeks_left):
    return {
        "season": weeks_left,
        "ir": min(4, weeks_left),
        "multi": min(3, weeks_left),
        "week": 1,
    }.get(length, 1)


# -------------------------------------------------------------- storage


SCHEMA = """
CREATE TABLE IF NOT EXISTS player_state (
    player_id INTEGER PRIMARY KEY,
    last_news REAL,
    injury_status TEXT,
    availability TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS seen_news (
    news_id TEXT PRIMARY KEY,
    player_id INTEGER,
    seen_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at REAL NOT NULL,
    kind TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    subject_id INTEGER,
    subject_name TEXT,
    subject_team TEXT,
    subject_position TEXT,
    headline TEXT,
    candidate_id INTEGER,
    candidate_name TEXT,
    candidate_position TEXT,
    candidate_team TEXT,
    candidate_availability TEXT,
    projection REAL,
    weekly_gain REAL,
    season_gain REAL,
    drop_id INTEGER,
    drop_name TEXT,
    bid REAL,
    note TEXT,
    action TEXT,
    action_detail TEXT
);
"""


def open_db(path=DEFAULT_DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def recent_events(connection, limit=50):
    rows = connection.execute(
        "SELECT * FROM events ORDER BY detected_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


# ------------------------------------------------------------- sources


def fetch_player_news(espn_id, http=None, limit=3):
    http = http or httpx
    try:
        response = http.get(
            NEWS_URL, params={"playerId": espn_id, "limit": limit}, timeout=20.0
        )
        response.raise_for_status()
        feed = response.json().get("feed") or []
    except Exception as error:
        log.warning("news for %s unavailable: %s", espn_id, error)
        return []
    items = []
    for item in feed:
        items.append(
            {
                "id": str(item.get("id")),
                "type": item.get("type") or "",
                "headline": item.get("headline") or item.get("description") or "",
                "story": item.get("story") or "",
                "published": item.get("published") or "",
            }
        )
    return items


def fetch_trending(http=None, hours=TRENDING_WINDOW_HOURS):
    http = http or httpx
    try:
        response = http.get(
            TRENDING_URL, params={"lookback_hours": hours, "limit": 50}, timeout=20.0
        )
        response.raise_for_status()
        return {str(row["player_id"]): int(row["count"]) for row in response.json()}
    except Exception as error:
        log.warning("Sleeper trending unavailable: %s", error)
        return {}


# Players whose adds in the last hour jumped against their recent pace:
# {sleeper id: (adds last hour, average adds per hour before)}.
def rushes(http=None):
    recent = fetch_trending(http, TRENDING_WINDOW_HOURS)
    baseline = fetch_trending(http, TRENDING_BASELINE_HOURS)
    found = {}
    for sleeper_id, adds in recent.items():
        per_hour = baseline.get(sleeper_id, adds) / TRENDING_BASELINE_HOURS
        if adds >= TRENDING_MIN_ADDS and adds >= TRENDING_MIN_SPIKE * max(per_hour, 1.0):
            found[sleeper_id] = (adds, per_hour)
    return found


# ESPN's "2026-09-23T21:48:08Z" as epoch milliseconds, like lastNewsDate.
def _published_ms(text):
    try:
        return calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")) * 1000.0
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------- signals


# Something that happened to one player: ("out", player, headline, length)
# or ("role"/"trending", player, headline, None). news_id dedupes it.
class Signal:
    def __init__(self, kind, player, headline, length=None, news_id=None):
        self.kind = kind
        self.player = player
        self.headline = headline
        self.length = length
        self.news_id = news_id


def is_relevant(player):
    return (
        player.percent_owned >= MIN_RELEVANT_OWNED
        or player.espn_projected_points >= MIN_RELEVANT_PROJECTION
    )


# The available players who gain from a signal: the player himself for a
# new role or a rush of adds, his same-position teammates for an absence
# (the one ESPN projects highest first, as the likely next man up).
def beneficiaries(signal, watchlist):
    subject = signal.player
    if signal.kind in ("role", "trending"):
        return [subject] if subject.availability in (C.STATUS_FREEAGENT, C.STATUS_WAIVERS) else []
    if subject.position not in SKILL_POSITIONS or not is_relevant(subject):
        return []
    mates = [
        p for p in watchlist
        if p.pro_team == subject.pro_team
        and p.position == subject.position
        and p.player_id != subject.player_id
        and p.availability in (C.STATUS_FREEAGENT, C.STATUS_WAIVERS)
    ]
    mates.sort(key=lambda p: (p.espn_projected_points, p.percent_owned), reverse=True)
    return mates[:MAX_BENEFICIARIES]


# What a player normally scores in a week. Once he is ruled out his current
# projections drop to zero, so his rest-of-season pace and ESPN's
# full-season projection are both considered, and the larger is used.
def typical_week(player):
    options = [player.projected_points, player.espn_projected_points]
    if player.ros_games:
        options.append(player.ros_points / player.ros_games)
    if player.season_projected_points:
        options.append(player.season_projected_points / 17.0)
    return max(options)


# A copy of the candidate as he would look with the opening: credited with
# a share of the absent starter's typical week, this week and for the weeks
# he is expected to miss.
def stand_in(candidate, signal, weeks_left):
    if signal.kind != "out":
        return candidate
    subject = signal.player
    typical = typical_week(subject)
    share = INHERIT_SHARE.get(candidate.position, 0.3) * typical
    weeks = missed_weeks(signal.length, weeks_left)
    own_week = candidate.ros_points / candidate.ros_games if candidate.ros_games else (
        candidate.projected_points
    )
    return replace(
        candidate,
        projected_points=max(candidate.projected_points, share),
        ros_points=candidate.ros_points + max(0.0, share - own_week) * weeks,
        ros_games=max(candidate.ros_games, 1),
    )


# ---------------------------------------------------------------- alerts


def notify_mac(title, message):
    try:
        subprocess.run(
            ["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"],
            timeout=10, check=False, capture_output=True,
        )
    except Exception as error:
        log.info("Mac notification failed: %s", error)


# Email one message through the Claude CLI's Gmail connector, the same way
# the morning report is sent. The message is fully written here; Claude only
# sends it, with the exact arguments given.
def notify_email(recipient, subject, body, html=None):
    if not recipient:
        return
    arguments = {"to": [recipient], "subject": subject, "body": body}
    if html:
        arguments["htmlBody"] = html
    prompt = (
        "Call mcp__claude_ai_Gmail__send_message once with exactly these arguments, "
        "unchanged, then reply 'sent'.\n" + json.dumps(arguments)
    )
    try:
        subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "mcp__claude_ai_Gmail__send_message"],
            timeout=180, check=False, capture_output=True,
        )
    except Exception as error:
        log.warning("alert email failed: %s", error)


# One line for a Mac notification.
def describe(event):
    where = "free agent" if event["candidate_availability"] == C.STATUS_FREEAGENT else "on waivers"
    return (
        f"{event['candidate_name']} ({event['candidate_position']}, {event['candidate_team']}), "
        f"{where}: {event['projection']:.1f} pts projected, "
        f"{event['weekly_gain']:+.1f} for your lineup. {event['headline']}"
    )


def _claim_command(event):
    on_waivers = event["candidate_availability"] == C.STATUS_WAIVERS
    command = f"python -m ffopt claim --add {event['candidate_id']}"
    if event["drop_id"]:
        command += f" --drop {event['drop_id']}"
    if on_waivers and event["bid"]:
        command += f" --bid {event['bid']:.0f}"
    if not on_waivers:
        command += " --free-agent"
    return command


# The move in a few words: "Add X, drop Y, bid $12 (waiver claim)".
def _move(event):
    on_waivers = event["candidate_availability"] == C.STATUS_WAIVERS
    text = f"Add {event['candidate_name']}"
    if event["drop_name"]:
        text += f", drop {event['drop_name']}"
    if on_waivers:
        text += f", bid ${event['bid']:.0f} (waiver claim)" if event["bid"] else " (waiver claim)"
    else:
        text += " (free agent: first come, first served)"
    return text


# How to make the suggested move yourself: the one command, and the button.
def claim_instructions(event):
    lead = "Suggested claim" if event["action"] == "suggested" else "If you want him"
    return (
        f"{lead}: {_move(event)}. Nothing has been done for you.\n"
        f"From the project folder: {_claim_command(event)}\n"
        f"Or use Add on the News tab: {NEWS_TAB_URL}"
    )


# ------------------------------------------------------------ the email

NEWS_TAB_URL = "http://127.0.0.1:8000"

LABELS = {
    "suggested": ("SUGGESTED CLAIM", "#1e7b34", "#e6f4ea"),
    "alerted": ("WORTH A LOOK", "#8a5a00", "#fff4e0"),
}


def _availability(event):
    return "Free agent" if event["candidate_availability"] == C.STATUS_FREEAGENT else "On waivers"


def email_subject(events):
    first = events[0]
    lead = "Claim now" if first["action"] == "suggested" else "Pickup alert"
    subject = f"{lead}: {first['candidate_name']} ({first['candidate_position']}, {first['candidate_team']})"
    if len(events) > 1:
        subject += f" + {len(events) - 1} more"
    return subject


def _summary(events):
    suggested = sum(1 for e in events if e["action"] == "suggested")
    looks = len(events) - suggested
    parts = []
    if suggested:
        parts.append(f"{suggested} suggested claim{'s' if suggested > 1 else ''}")
    if looks:
        parts.append(f"{looks} worth a look")
    return ", ".join(parts)


def email_text(events):
    lines = [f"PICKUP ALERT: {_summary(events)}", ""]
    for event in events:
        label = LABELS[event["action"]][0]
        lines += [
            label,
            f"{event['candidate_name']} ({event['candidate_position']}, {event['candidate_team']}), "
            f"{_availability(event).lower()}",
            f"News: {event['headline']}",
            f"Projected this week: {event['projection']:.1f} pts",
            f"Your lineup: {event['weekly_gain']:+.1f} this week, "
            f"{event['season_gain']:+.1f} rest of season",
            f"Move: {_move(event)}",
        ]
        if event["note"]:
            lines.append(f"Note: {event['note']}")
        lines += [f"Command: {_claim_command(event)}", "", "-" * 40, ""]
    lines += [
        f"Open the News tab: {NEWS_TAB_URL}",
        "Suggestions only. Nothing has been done for you.",
    ]
    return "\n".join(lines)


def email_html(events):
    esc = html_escape
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"
    muted = "#5b6573"
    cards = []
    for event in events:
        label, ink, fill = LABELS[event["action"]]
        stats = "".join(
            f'<td bgcolor="#f5f6f8" style="padding:8px 10px;background-color:#f5f6f8;border-radius:8px;width:33%;">'
            f'<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:{muted};">{name}</div>'
            f'<div style="font-size:18px;font-weight:600;color:#1b1f24;margin-top:2px;">{value}</div></td>'
            for name, value in (
                ("Projected pts", f"{event['projection']:.1f}"),
                ("Lineup gain", f"{event['weekly_gain']:+.1f}"),
                ("Season gain", f"{event['season_gain']:+.1f}"),
            )
        )
        note = (
            f'<p style="margin:12px 0 0;font-size:13px;line-height:1.45;color:{muted};">{esc(event["note"])}</p>'
            if event["note"] else ""
        )
        cards.append(
            f'<div style="background-color:#ffffff;border:1px solid #e3e6ea;border-radius:12px;padding:18px;margin:0 0 14px;">'
            f'<span style="display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;'
            f'color:{ink};background-color:{fill};border-radius:999px;padding:3px 10px;">{label}</span>'
            f'<div style="font-size:20px;font-weight:700;color:#1b1f24;margin:10px 0 2px;">{esc(event["candidate_name"])}</div>'
            f'<div style="font-size:13px;color:{muted};">{esc(event["candidate_position"])} &middot; '
            f'{esc(event["candidate_team"])} &middot; {_availability(event)}</div>'
            f'<p style="margin:12px 0;font-size:14px;line-height:1.5;color:#1b1f24;">'
            f'<b>News:</b> {esc(event["headline"])}</p>'
            f'<table role="presentation" cellspacing="6" cellpadding="0" style="width:100%;margin:0 -6px;">'
            f'<tr>{stats}</tr></table>'
            f'<div style="margin-top:12px;padding:10px 12px;border-left:3px solid {ink};background-color:#f5f6f8;'
            f'border-radius:0 8px 8px 0;font-size:14px;color:#1b1f24;"><b>Move:</b> {esc(_move(event))}</div>'
            f"{note}"
            f'<div style="margin-top:12px;font-family:Menlo,Consolas,monospace;font-size:12px;color:#1b1f24;'
            f'background-color:#f0f2f5;border-radius:6px;padding:8px 10px;word-break:break-all;">'
            f"{esc(_claim_command(event))}</div>"
            f"</div>"
        )
    return (
        f'<div style="background-color:#f5f6f8;padding:20px 12px;font-family:{font};">'
        f'<div style="max-width:560px;margin:0 auto;">'
        f'<div style="font-size:13px;color:{muted};margin:0 0 4px;">Fantasy news watcher</div>'
        f'<div style="font-size:22px;font-weight:700;color:#1b1f24;margin:0 0 14px;">'
        f"{esc(_summary(events)).capitalize()}</div>"
        f'{"".join(cards)}'
        f'<p style="font-size:13px;color:{muted};margin:6px 0 0;">'
        f'<a href="{NEWS_TAB_URL}" style="color:#1f6fd0;">Open the News tab</a> to add with one click '
        f"(it asks first). Suggestions only: nothing has been changed on your team.</p>"
        f"</div></div>"
    )


# ------------------------------------------------------------------ scan


class Scanner:
    """Runs one scan against the league and records what it finds."""

    def __init__(self, service, connection, http=None, notify=True, email_to=None, now=None):
        self.service = service
        self.connection = connection
        self.http = http or httpx
        self.notify = notify
        self.email_to = email_to
        self.now = now or time.time()

    # The 600 most-owned players as Player objects, with ESPN projections.
    def watchlist(self, league):
        records = self.service.client.fetch_free_agents(
            league.week,
            limit=WATCH_LIMIT,
            statuses=[C.STATUS_FREEAGENT, C.STATUS_WAIVERS, C.STATUS_ONTEAM],
        )
        byes = self.service.bye_weeks()
        players = []
        for record in records:
            raw = record.get("player") or record
            player = player_from_raw(
                raw, league.week, self.service.settings.season, bye_weeks=byes,
                final_week=league.settings.final_week,
            )
            player.availability = record.get("status") or C.STATUS_FREEAGENT
            player.fantasy_team_id = record.get("onTeamId") or 0
            player.last_news = float(raw.get("lastNewsDate") or 0)
            players.append(player)
        return players

    # Compare with the last scan: new news items and status changes.
    def signals(self, players):
        state = {
            row["player_id"]: dict(row)
            for row in self.connection.execute("SELECT * FROM player_state").fetchall()
        }
        first_scan = not state
        surnames = {surname(p) for p in players}
        found = []
        for player in players:
            before = state.get(player.player_id)
            if before and player.last_news > (before["last_news"] or 0):
                for item in fetch_player_news(player.player_id, http=self.http):
                    if _published_ms(item["published"]) <= (before["last_news"] or 0) - 60000:
                        continue
                    # Only player blurbs; articles are attached to every
                    # player they mention.
                    if item["type"] != "Rotowire":
                        continue
                    seen = self.connection.execute(
                        "SELECT 1 FROM seen_news WHERE news_id = ?", (item["id"],)
                    ).fetchone()
                    if seen:
                        continue
                    self.connection.execute(
                        "INSERT OR IGNORE INTO seen_news VALUES (?,?,?)",
                        (item["id"], player.player_id, self.now),
                    )
                    kind, length = classify(item["headline"], player, surnames)
                    if kind:
                        found.append(Signal(kind, player, item["headline"], length, item["id"]))
            elif (
                before
                and player.injury_status in UNAVAILABLE_STATUSES
                and before["injury_status"] not in UNAVAILABLE_STATUSES
            ):
                length = "ir" if player.injury_status == "INJURY_RESERVE" else "week"
                label = C.INJURY_STATUS_NAMES.get(player.injury_status, player.injury_status)
                found.append(Signal("out", player, f"ESPN now lists {player.name} as {label}.",
                                    length, f"status-{player.player_id}-{player.injury_status}"))

            self.connection.execute(
                "INSERT OR REPLACE INTO player_state VALUES (?,?,?,?,?)",
                (player.player_id, player.last_news, player.injury_status,
                 player.availability, self.now),
            )
        self.connection.commit()
        return [] if first_scan else found

    # Sleeper managers rushing to add a player who is free in this league.
    def trending_signals(self, players):
        spikes = rushes(http=self.http)
        if not spikes:
            return []
        ids = self.service.projection_service().ids() if self.service.projection_service() else None
        if ids is None:
            return []
        by_espn = {str(p.player_id): p for p in players}
        found = []
        day = time.strftime("%Y-%m-%d", time.localtime(self.now))
        for sleeper_id, (adds, per_hour) in spikes.items():
            player = by_espn.get(str(ids.espn_id("sleeper_id", sleeper_id) or ""))
            if player and player.availability in (C.STATUS_FREEAGENT, C.STATUS_WAIVERS):
                found.append(Signal(
                    "trending", player,
                    f"{adds:,} Sleeper adds of {player.name} in the last hour, "
                    f"against about {per_hour:,.0f} an hour before.",
                    news_id=f"trending-{player.player_id}-{day}",
                ))
        return found

    # Size one opening for one candidate and record it. Returns the event
    # dict, or None if it was already recorded.
    def record(self, signal, candidate, league, team):
        weeks_left = max(1, league.settings.final_week - league.week + 1)
        self.service._with_projections([candidate, signal.player], league)
        opening = stand_in(candidate, signal, weeks_left)
        evaluation = waivers.evaluate_pickup(
            team,
            opening,
            league.settings.starting_slots(),
            roster_limit=league.settings.active_roster_size,
            projection_fn=week_projection,
            season_fn=season_projection,
        )
        evaluation.weeks_left = weeks_left
        bid = 0.0
        if candidate.availability == C.STATUS_WAIVERS and league.settings.uses_faab:
            bid = waivers.suggest_faab_bid(
                evaluation.weekly_gain, team.faab_remaining,
                weeks_left=1 if evaluation.move_type == "stream" else weeks_left,
            )
        event = {
            "detected_at": self.now,
            "kind": signal.kind,
            "dedupe_key": f"{signal.news_id}:{candidate.player_id}",
            "subject_id": signal.player.player_id,
            "subject_name": signal.player.name,
            "subject_team": signal.player.pro_team,
            "subject_position": signal.player.position,
            "headline": signal.headline,
            "candidate_id": candidate.player_id,
            "candidate_name": candidate.name,
            "candidate_position": candidate.position,
            "candidate_team": candidate.pro_team,
            "candidate_availability": candidate.availability,
            "projection": round(opening.projected_points, 2),
            "weekly_gain": round(evaluation.weekly_gain, 2),
            "season_gain": round(evaluation.season_gain, 2),
            "drop_id": evaluation.drop_player.player_id if evaluation.drop_player else None,
            "drop_name": evaluation.drop_player.name if evaluation.drop_player else None,
            "bid": bid,
            "note": evaluation.note,
            "action": "none",
            "action_detail": "",
        }
        helps_me = (
            event["weekly_gain"] >= ALERT_MIN_WEEKLY or event["season_gain"] >= ALERT_MIN_SEASON
        )
        starter_level = event["projection"] >= STARTER_LEVEL.get(candidate.position, 99.0)
        worth_it = helps_me or starter_level
        if worth_it:
            event["action"] = "alerted"
            if not helps_me:
                event["note"] = (
                    f"Doesn't beat your starters, but about {event['projection']:.1f} points "
                    f"a week in this role is starter-level: grab him before a rival does, "
                    f"to stash or trade. " + (event["note"] or "")
                ).strip()
            elif self.is_strong(evaluation, league, team):
                event["action"] = "suggested"

        columns = ", ".join(event)
        marks = ", ".join("?" for _ in event)
        cursor = self.connection.execute(
            f"INSERT OR IGNORE INTO events ({columns}) VALUES ({marks})", tuple(event.values())
        )
        self.connection.commit()
        if cursor.rowcount == 0:
            return None
        return event

    # Strong enough to suggest claiming now: a real gain for your lineup, not
    # a one-week stream, and a drop who isn't in this week's best lineup.
    def is_strong(self, evaluation, league, team):
        strong = (
            evaluation.weekly_gain >= SUGGEST_MIN_WEEKLY
            or evaluation.season_gain >= SUGGEST_MIN_SEASON
        )
        if not strong or evaluation.move_type == "stream":
            return False
        if evaluation.drop_player is not None:
            assignments, _ = solve_lineup(
                [p for p in team.roster if p.lineup_slot != C.IR_SLOT],
                league.settings.starting_slots(),
            )
            starters = {p.player_id for _, p in assignments if p}
            if evaluation.drop_player.player_id in starters:
                return False
        return True

    # One Mac notification per opening, and one email for the whole scan,
    # strongest first, so a burst of Sunday news is one message.
    def alert(self, events):
        events = [e for e in events if e["action"] in ("suggested", "alerted")]
        if not self.notify or not events:
            return
        events.sort(key=lambda e: (e["action"] != "suggested", -e["weekly_gain"], -e["projection"]))
        for event in events:
            verb = "Claim now" if event["action"] == "suggested" else "Pick up"
            notify_mac(f"{verb}: {event['candidate_name']}", describe(event))

        notify_email(
            self.email_to, email_subject(events), email_text(events), email_html(events)
        )

    def already_recorded(self, signal, candidate):
        return self.connection.execute(
            "SELECT 1 FROM events WHERE dedupe_key = ?",
            (f"{signal.news_id}:{candidate.player_id}",),
        ).fetchone() is not None

    # One scan. The frequent part (watch list, news, rushes) uses ESPN alone;
    # the full projections are loaded only when there is an opening to size.
    def run(self):
        basic = self.service.load_league_basic()
        players = self.watchlist(basic)
        signals = self.signals(players) + self.trending_signals(players)
        openings = [
            (s, c) for s in signals for c in beneficiaries(s, players)
            if not self.already_recorded(s, c)
        ]
        events = []
        if openings:
            league = self.service.load_league()
            team = self.service.my_team()
            for signal, candidate in openings:
                event = self.record(signal, candidate, league, team)
                if event:
                    events.append(event)
        self.alert(events)
        return {"scanned": len(players), "signals": len(signals), "events": events}
