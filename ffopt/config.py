"""
Configuration for the league connection.

Everything the app needs to talk to ESPN comes from environment variables,
optionally loaded from a .env file in the project root. Nothing secret is
ever written to disk by this app.
"""

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


# Parse a .env file into a plain dict. This is a deliberately small parser:
# KEY=value lines, # comments, optional surrounding quotes. No interpolation.
def parse_env_file(path):
    values = {}
    if not Path(path).exists():
        return values

    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value

    return values


# Load the .env file into os.environ without clobbering variables that are
# already set. Real environment variables always win over the file.
def load_env_file(path=DEFAULT_ENV_FILE):
    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)


# ESPN writes the SWID cookie wrapped in braces. Users copy it both ways, so
# normalize to the braced form the API expects.
def normalize_swid(swid):
    if not swid:
        return ""
    swid = swid.strip()
    if not swid.startswith("{"):
        swid = "{" + swid
    if not swid.endswith("}"):
        swid = swid + "}"
    return swid


# Guess the current NFL season year. The season is named for the calendar
# year it starts in, so January through July still belongs to the prior year.
def default_season(today=None):
    import datetime

    today = today or datetime.date.today()
    if today.month < 8:
        return today.year - 1
    return today.year


@dataclass
class Settings:
    """Resolved configuration for one league connection."""

    league_id: str = ""
    season: int = 0
    team_id: int = 0
    swid: str = ""
    espn_s2: str = ""
    read_only: bool = False
    request_timeout: float = 20.0
    cache_ttl_seconds: float = 90.0
    anthropic_api_key: str = ""
    intel_ttl_seconds: float = 21600.0
    intel_model: str = "claude-sonnet-5"
    use_nflverse: bool = False
    # Where player projections come from: "espn" alone, or "consensus", a
    # blend of ESPN, Sleeper, and FantasyPros (see ffopt/projections).
    projection_source: str = "espn"
    fantasypros_api_key: str = ""
    projections_ttl_seconds: float = 3 * 3600.0
    # Run our own model each week and nudge the blend toward it where the
    # backtest showed that helps.
    use_model: bool = False
    # News watcher (ffopt/news.py): email alerts to this address. The
    # watcher suggests claims but never makes them.
    news_email: str = ""

    # True when we have enough to read a league at all.
    @property
    def is_configured(self):
        return bool(self.league_id) and bool(self.season)

    # Private leagues need both cookies. Public leagues read fine without them.
    @property
    def has_credentials(self):
        return bool(self.swid) and bool(self.espn_s2)

    # Writes need cookies, a team id, and write mode enabled.
    @property
    def can_write(self):
        return self.has_credentials and bool(self.team_id) and not self.read_only

    # Explain, in plain words, what is missing. Empty list means ready.
    def missing_fields(self):
        missing = []
        if not self.league_id:
            missing.append("ESPN_LEAGUE_ID")
        if not self.season:
            missing.append("ESPN_SEASON")
        return missing

    # A redacted view safe to send to the browser or log.
    def public_dict(self):
        return {
            "league_id": self.league_id,
            "season": self.season,
            "team_id": self.team_id,
            "has_credentials": self.has_credentials,
            "read_only": self.read_only,
            "can_write": self.can_write,
            "llm_enabled": bool(self.anthropic_api_key),
            "missing_fields": self.missing_fields(),
        }


# Build Settings from the environment, loading .env first.
def load_settings(env=None, env_file=DEFAULT_ENV_FILE):
    if env is None:
        load_env_file(env_file)
        env = os.environ

    def as_int(name, fallback=0):
        raw = (env.get(name) or "").strip()
        if not raw:
            return fallback
        try:
            return int(raw)
        except ValueError:
            return fallback

    def as_bool(name, fallback=False):
        raw = (env.get(name) or "").strip().lower()
        if not raw:
            return fallback
        return raw in ("1", "true", "yes", "on")

    def as_float(name, fallback):
        raw = (env.get(name) or "").strip()
        if not raw:
            return fallback
        try:
            return float(raw)
        except ValueError:
            return fallback

    return Settings(
        league_id=(env.get("ESPN_LEAGUE_ID") or "").strip(),
        season=as_int("ESPN_SEASON", default_season()),
        team_id=as_int("ESPN_TEAM_ID", 0),
        swid=normalize_swid(env.get("ESPN_SWID")),
        espn_s2=(env.get("ESPN_S2") or "").strip(),
        read_only=as_bool("FFOPT_READ_ONLY", False),
        request_timeout=as_float("FFOPT_TIMEOUT", 20.0),
        cache_ttl_seconds=as_float("FFOPT_CACHE_TTL", 90.0),
        anthropic_api_key=(env.get("ANTHROPIC_API_KEY") or "").strip(),
        intel_ttl_seconds=as_float("FFOPT_INTEL_TTL", 21600.0),
        intel_model=(env.get("FFOPT_INTEL_MODEL") or "claude-sonnet-5").strip(),
        use_nflverse=as_bool("FFOPT_NFLVERSE", True),
        projection_source=(env.get("FFOPT_PROJECTIONS") or "consensus").strip().lower(),
        fantasypros_api_key=(env.get("FANTASYPROS_API_KEY") or "").strip(),
        projections_ttl_seconds=as_float("FFOPT_PROJECTIONS_TTL", 3 * 3600.0),
        use_model=as_bool("FFOPT_MODEL", True),
        news_email=(
            (env.get("FFOPT_REPORT_EMAIL") or "").strip()
            if as_bool("FFOPT_NEWS_EMAIL", True) else ""
        ),
    )
