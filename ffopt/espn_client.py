"""
HTTP client for ESPN's fantasy football API.

ESPN has no public documentation for this API, but the endpoints below are
the ones its own web app uses. Private leagues require two cookies, SWID and
espn_s2, which the user copies out of their browser. Reads go to the read
host, roster moves and waiver claims go to the write host.
"""

import json

import httpx

from . import constants as C


READ_HOST = "https://lm-api-reads.fantasy.espn.com"
WRITE_HOST = "https://lm-api-writes.fantasy.espn.com"
API_PATH = "/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}"
PRO_TEAMS_PATH = "/apis/v3/games/ffl/seasons/{season}"


class EspnError(Exception):
    """Any failure talking to ESPN, with a human-readable explanation."""

    def __init__(self, message, status_code=None, url=None, body=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.url = url
        self.body = body

    def to_dict(self):
        return {
            "error": self.message,
            "status_code": self.status_code,
            "url": self.url,
        }


class EspnAuthError(EspnError):
    """Cookies are missing, expired, or not valid for this league."""


class EspnNotFoundError(EspnError):
    """The league or season does not exist, or is not visible to this user."""


class EspnClient:
    """Thin wrapper around the ESPN endpoints, one instance per league."""

    def __init__(self, settings, transport=None):
        self.settings = settings
        self._client = httpx.Client(
            timeout=settings.request_timeout,
            cookies=self._cookies(),
            headers=self._base_headers(),
            transport=transport,
            follow_redirects=True,
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # Private leagues authenticate purely with these two cookies.
    def _cookies(self):
        cookies = {}
        if self.settings.swid:
            cookies["SWID"] = self.settings.swid
        if self.settings.espn_s2:
            cookies["espn_s2"] = self.settings.espn_s2
        return cookies

    # ESPN rejects requests that do not look like they came from its web app.
    def _base_headers(self):
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "X-Fantasy-Source": "kona",
            "X-Fantasy-Platform": "kona-PROD--1",
            "Referer": "https://fantasy.espn.com/",
        }

    def _league_url(self, host):
        return host + API_PATH.format(
            season=self.settings.season, league_id=self.settings.league_id
        )

    # Turn an HTTP failure into an exception that says what to do about it.
    def _raise_for_status(self, response, url):
        if response.status_code < 400:
            return

        body = response.text[:600]

        if response.status_code in (401, 403):
            raise EspnAuthError(
                "ESPN rejected the request. For a private league, set ESPN_SWID "
                "and ESPN_S2 from your browser cookies, and make sure the "
                "account owns or can view this league.",
                status_code=response.status_code,
                url=url,
                body=body,
            )
        if response.status_code == 404:
            raise EspnNotFoundError(
                f"ESPN has no league {self.settings.league_id} for season "
                f"{self.settings.season}. Check the league id and the season year.",
                status_code=response.status_code,
                url=url,
                body=body,
            )
        raise EspnError(
            f"ESPN returned HTTP {response.status_code}: {body}",
            status_code=response.status_code,
            url=url,
            body=body,
        )

    # GET the league resource with one or more views. The x-fantasy-filter
    # header is how ESPN filters and sorts the player pool.
    def get_league(self, views, params=None, fantasy_filter=None):
        url = self._league_url(READ_HOST)

        query = list(params.items()) if params else []
        for view in views:
            query.append(("view", view))

        headers = {}
        if fantasy_filter:
            headers["X-Fantasy-Filter"] = json.dumps(fantasy_filter)

        try:
            response = self._client.get(url, params=query, headers=headers)
        except httpx.HTTPError as exc:
            raise EspnError(f"Could not reach ESPN: {exc}", url=url) from exc

        self._raise_for_status(response, url)

        try:
            payload = response.json()
        except ValueError as exc:
            raise EspnError(
                "ESPN returned a non-JSON response. This usually means the "
                "espn_s2 cookie has expired and ESPN served a login page.",
                url=url,
            ) from exc

        # Some season/league combinations come back wrapped in a list.
        if isinstance(payload, list):
            return payload[0] if payload else {}
        return payload

    # League settings, teams, rosters, and schedule in one round trip.
    def fetch_league_snapshot(self, week=None):
        params = {}
        if week:
            params["scoringPeriodId"] = week

        return self.get_league(
            [
                "mSettings",
                "mTeam",
                "mRoster",
                "mMatchupScore",
                "mStandings",
            ],
            params=params,
        )

    # Bye weeks live on the season-level pro team schedule, not the league.
    def fetch_bye_weeks(self):
        url = READ_HOST + PRO_TEAMS_PATH.format(season=self.settings.season)
        try:
            response = self._client.get(url, params={"view": "proTeamSchedules_wl"})
        except httpx.HTTPError:
            return {}

        if response.status_code >= 400:
            return {}

        try:
            payload = response.json()
        except ValueError:
            return {}

        if isinstance(payload, list):
            payload = payload[0] if payload else {}

        bye_weeks = {}
        pro_teams = (payload.get("settings") or {}).get("proTeams") or []
        for team in pro_teams:
            bye = team.get("byeWeek")
            if bye:
                bye_weeks[team.get("id")] = bye
        return bye_weeks

    # The pool of players not on any roster, ordered by ownership. ESPN caps
    # this list, so limit is a real limit, not a page size.
    def fetch_free_agents(self, week, limit=150, slot_ids=None, statuses=None):
        statuses = statuses or [C.STATUS_FREEAGENT, C.STATUS_WAIVERS]

        player_filter = {
            "filterStatus": {"value": statuses},
            "limit": limit,
            "offset": 0,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            "filterRanksForScoringPeriodIds": {"value": [week]},
        }
        if slot_ids:
            player_filter["filterSlotIds"] = {"value": list(slot_ids)}

        payload = self.get_league(
            ["kona_player_info"],
            params={"scoringPeriodId": week},
            fantasy_filter={"players": player_filter},
        )
        return payload.get("players") or []

    # Waiver claims and trades that have been submitted but not processed.
    def fetch_pending_transactions(self):
        payload = self.get_league(["mPendingTransactions", "mTransactions2"])
        return payload.get("pendingTransactions") or []

    # POST a transaction. Every roster move in ESPN, including a simple
    # bench/start swap, is a transaction against this one endpoint.
    def submit_transaction(self, payload):
        if self.settings.read_only:
            raise EspnError(
                "This app is running in read-only mode. Unset FFOPT_READ_ONLY "
                "to allow roster moves."
            )
        if not self.settings.has_credentials:
            raise EspnAuthError(
                "Roster moves need ESPN_SWID and ESPN_S2 cookies to be set."
            )

        url = self._league_url(WRITE_HOST) + "/transactions/"

        try:
            response = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise EspnError(f"Could not reach ESPN: {exc}", url=url) from exc

        self._raise_for_status(response, url)

        try:
            return response.json()
        except ValueError:
            return {"status": "ok", "raw": response.text[:400]}


# Build the transaction body for a set of lineup slot changes. ESPN wants
# every moved player listed with the slot they came from and the slot they
# are going to, applied as one atomic transaction.
def build_lineup_payload(team_id, member_id, week, moves):
    items = []
    for move in moves:
        items.append(
            {
                "playerId": int(move["player_id"]),
                "type": "LINEUP",
                "fromLineupSlotId": int(move["from_slot"]),
                "toLineupSlotId": int(move["to_slot"]),
            }
        )

    return {
        "isLeagueManager": False,
        "teamId": int(team_id),
        "type": "ROSTER",
        "memberId": member_id,
        "scoringPeriodId": int(week),
        "executionType": "EXECUTE",
        "items": items,
    }


# Build the transaction body for adding a player, optionally dropping one to
# make room. Type WAIVER queues a claim for waiver processing and carries a
# FAAB bid; type FREEAGENT takes an unclaimed player immediately.
def build_add_drop_payload(
    team_id,
    member_id,
    week,
    add_player_id=None,
    drop_player_id=None,
    bid_amount=None,
    transaction_type="WAIVER",
    add_to_slot=C.BENCH_SLOT,
    drop_from_slot=C.BENCH_SLOT,
):
    if add_player_id is None and drop_player_id is None:
        raise ValueError("A claim needs a player to add, a player to drop, or both.")

    items = []
    if add_player_id is not None:
        items.append(
            {
                "playerId": int(add_player_id),
                "type": "ADD",
                "toTeamId": int(team_id),
                "toLineupSlotId": int(add_to_slot),
            }
        )
    if drop_player_id is not None:
        items.append(
            {
                "playerId": int(drop_player_id),
                "type": "DROP",
                "fromTeamId": int(team_id),
                "fromLineupSlotId": int(drop_from_slot),
            }
        )

    payload = {
        "isLeagueManager": False,
        "teamId": int(team_id),
        "type": transaction_type,
        "memberId": member_id,
        "scoringPeriodId": int(week),
        "executionType": "EXECUTE",
        "items": items,
    }

    # FAAB leagues attach the bid to the transaction itself.
    if transaction_type == "WAIVER" and bid_amount is not None:
        payload["bidAmount"] = float(bid_amount)

    return payload
