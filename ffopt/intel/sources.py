"""
Fetchers for public waiver wire advice.

Two kinds of source:
  * Trending data: Sleeper publishes how many leagues added each player.
  * Article feeds: RSS feeds from fantasy sites. We keep the items that look
    like waiver advice and read their text.

Every source fails on its own. A dead or blocked site is reported in the
source status list and never stops the others. Paywalled sites (The Athletic)
only give us public headlines and summaries, which is all we read from them.
"""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# name, feed url, whether the full article text is public
FEEDS = [
    ("FantasyPros", "https://www.fantasypros.com/feed/", True),
    ("RotoBaller", "https://www.rotoballer.com/feed", True),
    ("The Athletic", "https://www.nytimes.com/athletic/rss/nfl/", False),
    ("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/", True),
    ("Yahoo Sports", "https://sports.yahoo.com/nfl/rss.xml", True),
    ("4for4", "https://www.4for4.com/rss.xml", True),
    ("PFF", "https://www.pff.com/feed", True),
    ("Fantasy Footballers", "https://www.thefantasyfootballers.com/feed/", True),
]

SLEEPER_TRENDING = (
    "https://api.sleeper.app/v1/players/nfl/trending/add"
    "?lookback_hours=48&limit=100"
)
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"

# An item is waiver advice if its headline says so.
WAIVER_WORDS = (
    "waiver", "pickup", "pick up", "pick-up", "streamer", "stash",
    "add/drop", "adds", "free agent", "sleeper", "breakout", "trending",
    "wire",
)

MAX_ARTICLES_PER_SOURCE = 4
MAX_ARTICLE_CHARS = 30000


class IntelError(Exception):
    pass


@dataclass
class Article:
    source: str
    title: str
    url: str
    text: str
    full_text: bool = True


@dataclass
class TrendingAdd:
    name: str
    team: str
    position: str
    count: int
    rank: int


@dataclass
class SourceStatus:
    source: str
    ok: bool
    count: int = 0
    note: str = ""
    articles: list = field(default_factory=list)

    def to_dict(self):
        return {
            "source": self.source,
            "ok": self.ok,
            "count": self.count,
            "note": self.note,
            "articles": self.articles,
        }


# Strip tags and collapse whitespace.
def html_to_text(markup):
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "form"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def looks_like_waiver_advice(title):
    lowered = (title or "").lower()
    return any(word in lowered for word in WAIVER_WORDS)


# Tag names in feeds carry namespaces, so compare on the local part only.
def local_name(tag):
    return tag.rsplit("}", 1)[-1]


# Parse an RSS document into (title, link, summary_html) tuples.
def parse_feed(xml_text):
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as exc:
        raise IntelError(f"Feed was not valid XML: {exc}") from exc

    for node in root.iter():
        if local_name(node.tag) not in ("item", "entry"):
            continue

        title = link = ""
        summary = ""
        for child in node:
            name = local_name(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = (child.text or child.attrib.get("href") or "").strip()
            elif name in ("encoded", "content"):
                summary = child.text or summary
            elif name in ("description", "summary") and not summary:
                summary = child.text or ""
        if title and link:
            items.append((title, link, summary))
    return items


class IntelClient:
    """Small HTTP wrapper shared by every source. Takes a transport so tests
    never touch the network."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        self._client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
            transport=transport,
        )

    def close(self):
        self._client.close()

    def get(self, url, timeout=None):
        try:
            response = self._client.get(url, timeout=timeout or self.settings.request_timeout)
        except httpx.HTTPError as exc:
            raise IntelError(f"Could not reach {url}: {exc}") from exc
        if response.status_code >= 400:
            raise IntelError(f"{url} answered {response.status_code}")
        return response

    # Read the waiver-looking articles from one feed.
    def fetch_feed(self, name, url, full_text_public):
        response = self.get(url)
        items = parse_feed(response.text)
        wanted = [i for i in items if looks_like_waiver_advice(i[0])]
        wanted = wanted[:MAX_ARTICLES_PER_SOURCE]

        articles = []
        for title, link, summary in wanted:
            if "premium" in title.lower():
                continue

            text = html_to_text(summary)
            full = False
            # Feed summaries are short. Pull the article body when the site
            # publishes it openly, and keep whichever is longer.
            if full_text_public:
                try:
                    body = html_to_text(self.get(link).text)
                    if len(body) > len(text):
                        text = body
                        full = True
                except IntelError:
                    pass

            articles.append(
                Article(
                    source=name,
                    title=title,
                    url=link,
                    text=text[:MAX_ARTICLE_CHARS],
                    full_text=full,
                )
            )
        return articles

    # Sleeper's most-added players, resolved to names.
    def fetch_trending(self):
        adds = self.get(SLEEPER_TRENDING).json()
        players = self.get(SLEEPER_PLAYERS, timeout=60).json()

        results = []
        for rank, entry in enumerate(adds, start=1):
            info = players.get(str(entry.get("player_id"))) or {}
            name = info.get("full_name") or (
                f"{info.get('first_name', '')} {info.get('last_name', '')}".strip()
            )
            if not name:
                continue
            results.append(
                TrendingAdd(
                    name=name,
                    team=info.get("team") or "",
                    position=info.get("position") or "",
                    count=int(entry.get("count") or 0),
                    rank=rank,
                )
            )
        return results


# Pull everything, one source at a time, recording what worked.
def gather_all(client, feeds=FEEDS):
    articles = []
    trending = []
    statuses = []

    try:
        trending = client.fetch_trending()
        statuses.append(
            SourceStatus("Sleeper trending", True, count=len(trending))
        )
    except (IntelError, ValueError) as exc:
        statuses.append(SourceStatus("Sleeper trending", False, note=str(exc)))

    for name, url, public in feeds:
        try:
            found = client.fetch_feed(name, url, public)
        except IntelError as exc:
            statuses.append(SourceStatus(name, False, note=str(exc)))
            continue

        note = "" if public else "Paywalled: headlines and summaries only."
        if not found:
            note = (note + " No waiver articles in the feed right now.").strip()
        statuses.append(
            SourceStatus(
                name,
                True,
                count=len(found),
                note=note,
                articles=[{"title": a.title, "url": a.url} for a in found],
            )
        )
        articles.extend(found)

    return articles, trending, statuses, time.time()
