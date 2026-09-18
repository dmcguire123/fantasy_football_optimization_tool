"""Waiver intelligence: scraping, matching, consensus, bids, analysis, API."""

import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import make_transport
from fastapi.testclient import TestClient

from ffopt import api, waivers
from ffopt.espn_client import EspnClient
from ffopt.intel import analysis, bids, consensus, matching, sources
from ffopt.intel.service import IntelService
from ffopt.league import LeagueService


FEED = """<?xml version="1.0"?><rss version="2.0"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Site</title>
<item><title>Week 5 Waiver Wire Pickups</title><link>https://example.com/a1</link>
<description>Grab Waiver Gem RB before anyone else.</description></item>
<item><title>Injury report</title><link>https://example.com/a2</link>
<description>Nothing here.</description></item>
<item><title>Waiver Wire (Premium Content)</title><link>https://example.com/a3</link>
<description>Paid.</description></item>
</channel></rss>"""


# Serve fake feeds and Sleeper data so no test touches the network.
def web_transport():
    def handler(request):
        url = str(request.url)
        if "trending/add" in url:
            return httpx.Response(200, json=[{"player_id": "1", "count": 900}])
        if url.endswith("/v1/players/nfl"):
            return httpx.Response(
                200,
                json={"1": {"full_name": "Waiver Gem RB", "team": "X", "position": "RB"}},
            )
        if url == "https://example.com/feed":
            return httpx.Response(200, text=FEED)
        if url == "https://example.com/a1":
            return httpx.Response(200, text="<html><body><p>Waiver Gem RB is a must add this week, and here is the long explanation why.</p></body></html>")
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


class FakeLlm:
    def __init__(self, text):
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)]))


@pytest.fixture
def pool(service, league):
    return service.free_agents(week=league.week, limit=50)


# ---------- sources ----------

def test_parse_feed_returns_title_link_and_summary():
    items = sources.parse_feed(FEED)
    assert items[0] == ("Week 5 Waiver Wire Pickups", "https://example.com/a1",
                        "Grab Waiver Gem RB before anyone else.")
    assert len(items) == 3


def test_parse_feed_rejects_garbage():
    with pytest.raises(sources.IntelError):
        sources.parse_feed("not xml at all")


def test_only_waiver_headlines_are_kept():
    assert sources.looks_like_waiver_advice("Week 5 Waiver Wire Pickups")
    assert not sources.looks_like_waiver_advice("Injury report")


def test_gather_all_reports_each_source_and_skips_premium(settings):
    client = sources.IntelClient(settings, transport=web_transport())
    feeds = [("Good", "https://example.com/feed", True), ("Dead", "https://example.com/missing", True)]
    articles, trending, statuses, _ = sources.gather_all(client, feeds=feeds)

    by_name = {s.source: s for s in statuses}
    assert by_name["Sleeper trending"].ok and trending[0].name == "Waiver Gem RB"
    assert by_name["Good"].ok and by_name["Good"].count == 1
    assert not by_name["Dead"].ok
    assert "must add" in articles[0].text


def test_paywalled_source_only_uses_the_feed_summary(settings):
    client = sources.IntelClient(settings, transport=web_transport())
    found = client.fetch_feed("Athletic", "https://example.com/feed", False)
    assert found[0].full_text is False
    assert "before anyone else" in found[0].text


# ---------- matching ----------

def test_names_match_across_suffixes_and_punctuation():
    assert matching.normalize_name("Chris Godwin Jr.") == "chris godwin"
    assert matching.normalize_name("Ja'Marr Chase") == "jamarr chase"


def test_find_mentions_needs_a_whole_name(pool):
    index = matching.build_index(pool)
    hits = matching.find_mentions("We love Waiver Gem RB this week.", index)
    assert [p.name for p, _ in hits] == ["Waiver Gem RB"]
    assert matching.find_mentions("Waiver Gem RBs everywhere", index) == []


# ---------- consensus ----------

def test_consensus_ranks_players_named_by_more_sources_higher(pool):
    article = lambda src, text: sources.Article(src, "Waiver picks", f"https://{src}.com", text)
    articles = [
        article("A", "Add Waiver Gem RB and Deep Sleeper TE."),
        article("B", "Waiver Gem RB is the top add."),
    ]
    ranked = consensus.build_consensus(pool, articles, [])

    assert ranked[0]["player"]["name"] == "Waiver Gem RB"
    assert ranked[0]["source_count"] == 2
    assert ranked[0]["rank"] == 1 and ranked[1]["rank"] == 2


def test_trending_adds_count_as_a_source(pool):
    trending = [sources.TrendingAdd("Waiver Gem RB", "X", "RB", 900, 1)]
    ranked = consensus.build_consensus(pool, [], trending)
    assert ranked[0]["sources"] == ["Sleeper trending"]
    assert ranked[0]["trending_rank"] == 1


# ---------- bids ----------

def advice(league, **overrides):
    args = dict(weekly_gain=4.0, faab_remaining=100.0, weeks_left=10,
                source_count=1, position="RB", league=league, my_team_id=1)
    args.update(overrides)
    return bids.bid_advice(**args)


def test_bid_is_a_percent_of_remaining_budget(league):
    result = advice(league)
    assert result["dollars"] >= 1
    assert result["percent"] == pytest.approx(result["dollars"], abs=0.1)


def test_more_sources_never_lower_the_bid(league):
    low = advice(league, source_count=1)["dollars"]
    high = advice(league, source_count=4)["dollars"]
    assert high >= low


def test_bid_never_exceeds_half_the_budget(league):
    assert advice(league, weekly_gain=50.0, source_count=9)["percent"] <= 50.0


def test_kickers_and_defenses_get_small_bids(league):
    assert advice(league, weekly_gain=20.0, position="D/ST")["percent"] <= 5.0
    assert advice(league, weekly_gain=20.0, position="K")["percent"] <= 3.0


def test_no_budget_means_no_bid(league):
    assert advice(league, faab_remaining=0.0)["dollars"] == 0


def test_no_gain_and_no_buzz_means_no_bid(league):
    assert advice(league, weekly_gain=0.0, source_count=0)["dollars"] == 0


# ---------- analysis ----------

CANDIDATES = [{"player_id": 401, "name": "Waiver Gem RB", "weekly_gain": 6.0,
               "consensus_score": 4.0, "source_count": 2, "drop": "Bench Guy"}]


def test_no_key_falls_back_to_heuristic(settings):
    results, mode, note = analysis.analyze(settings, CANDIDATES, {})
    assert mode == "heuristic" and "ANTHROPIC_API_KEY" in note
    assert results[401]["priority"] >= 4


def test_valid_model_reply_is_used(settings):
    reply = json.dumps({"players": [{"player_id": 401, "priority": 5, "reasoning": "Do it."}]})
    results, mode, _ = analysis.analyze(settings, CANDIDATES, {}, client=FakeLlm(f"```json\n{reply}\n```"))
    assert mode == "llm" and results[401]["reasoning"] == "Do it."


def test_bad_model_reply_falls_back(settings):
    results, mode, note = analysis.analyze(settings, CANDIDATES, {}, client=FakeLlm("I cannot help"))
    assert mode == "heuristic" and 401 in results


def test_out_of_range_priority_is_rejected(settings):
    reply = json.dumps({"players": [{"player_id": 401, "priority": 99, "reasoning": "x"}]})
    _, mode, _ = analysis.analyze(settings, CANDIDATES, {}, client=FakeLlm(reply))
    assert mode == "heuristic"


def test_model_cannot_invent_players(settings):
    reply = json.dumps({"players": [{"player_id": 999, "priority": 5, "reasoning": "x"}]})
    results, _, _ = analysis.analyze(settings, CANDIDATES, {}, client=FakeLlm(reply))
    assert 999 not in results


# ---------- drop tie-break ----------

def test_a_zero_gain_pickup_never_suggests_cutting_a_starter(service, league, starting_slots):
    team = league.team_by_id(1)
    pool = service.free_agents(week=league.week, limit=50)
    worthless = min(pool, key=lambda p: p.effective_projection)

    evaluation = waivers.evaluate_pickup(team, worthless, starting_slots, roster_limit=len(team.roster))
    if evaluation.weekly_gain == 0 and evaluation.drop_player:
        assert not evaluation.drop_player.is_starting


# ---------- API ----------

@pytest.fixture
def http(settings, requests_log):
    espn = EspnClient(settings, transport=make_transport(requests_log))
    league_service = LeagueService(settings, client=espn)
    api._service = league_service
    api._intel = IntelService(
        league_service,
        transport=web_transport(),
        feeds=[("Test Feed", "https://example.com/feed", True)],
    )
    with TestClient(api.app) as test_client:
        yield test_client
    api._service = None
    api._intel = None


def test_consensus_endpoint(http):
    body = http.get("/api/waivers/consensus").json()
    names = [p["player"]["name"] for p in body["players"]]
    assert "Waiver Gem RB" in names
    assert any(s["source"] == "Test Feed" and s["ok"] for s in body["sources"])


def test_rosters_endpoint_lists_every_team_with_needs(http):
    body = http.get("/api/waivers/rosters").json()
    assert len(body["teams"]) == 3
    assert all("needs" in t for t in body["teams"])
    assert body["my_team_id"] == 1


def test_targets_endpoint_returns_bid_percent(http):
    body = http.get("/api/waivers/targets").json()
    assert body["analysis"] == "heuristic"
    assert body["targets"]
    top = body["targets"][0]
    assert 0 <= top["bid_percent"] <= 50
    assert top["bid_dollars"] >= 0 and 1 <= top["priority"] <= 5


def test_reading_intel_never_writes_to_espn(http, requests_log):
    http.get("/api/waivers/targets")
    assert not [r for r in requests_log if r.method == "POST"]


# ---------- stash ----------

from ffopt import constants as C
from ffopt.intel import stash
from ffopt.league import parse_settings
from ffopt.models import Player

QB_RB_WR = [0, 2, 4]


def mk(pid, name, pos, team, proj, slot=C.BENCH_SLOT, status="ACTIVE", season=0.0):
    return Player(
        player_id=pid, name=name, position=pos, pro_team=team,
        eligible_slots=C.FALLBACK_ELIGIBLE_SLOTS[pos], lineup_slot=slot,
        projected_points=proj, season_projected_points=season, injury_status=status,
    )


def ctx_for(roster, others=(), byes=None, week=2, final=17):
    team = SimpleNamespace(roster=roster)
    rivals = [SimpleNamespace(roster=r) for r in others]
    return stash.build_context(team, rivals, QB_RB_WR, week, final, byes or {})


def base_roster():
    return [
        mk(1, "Star QB", "QB", "AAA", 20, slot=0),
        mk(2, "Star RB", "RB", "SF", 20, slot=2),
        mk(3, "Star WR", "WR", "CCC", 15, slot=4),
        mk(4, "Weak Bench RB", "RB", "DDD", 3),
    ]


def test_typical_week_spreads_the_season_projection():
    assert stash.typical_week(mk(9, "A", "RB", "X", 5, season=170)) == pytest.approx(10.0)
    assert stash.typical_week(mk(9, "A", "RB", "X", 5)) == 5


def test_only_same_team_same_position_backs_are_handcuffs():
    starters = [mk(2, "Star RB", "RB", "SF", 20, slot=2)]
    assert stash.handcuff_targets(mk(9, "Backup", "RB", "SF", 4), starters)
    assert not stash.handcuff_targets(mk(9, "Other", "RB", "NYG", 4), starters)
    assert not stash.handcuff_targets(mk(9, "WR", "WR", "SF", 4), starters)


def test_a_handcuff_is_worth_more_than_the_same_player_elsewhere():
    ctx = ctx_for(base_roster())
    handcuff = stash.profile(mk(9, "Backup", "RB", "SF", 4), ctx)
    other = stash.profile(mk(10, "Backup", "RB", "NYG", 4), ctx)
    assert handcuff.injury_cover > other.injury_cover
    assert handcuff.handcuff_of == ["Star RB"]


def test_a_hurt_starter_raises_the_value_of_cover():
    healthy = stash.profile(mk(9, "Backup", "RB", "SF", 4), ctx_for(base_roster()))
    roster = base_roster()
    roster[1].injury_status = "OUT"
    hurt = stash.profile(mk(9, "Backup", "RB", "SF", 4), ctx_for(roster))
    assert hurt.injury_cover > healthy.injury_cover


def test_a_player_who_is_out_himself_is_discounted():
    ctx = ctx_for(base_roster())
    well = stash.profile(mk(9, "Backup", "RB", "SF", 4), ctx)
    hurt = stash.profile(mk(9, "Backup", "RB", "SF", 4, status="OUT"), ctx)
    assert hurt.score < well.score


def test_bye_week_cover_counts_when_a_starter_is_off():
    roster = base_roster()
    roster[3] = mk(4, "Bench RB", "RB", "DDD", 0)
    ctx = ctx_for(roster, byes={"SF": 7})
    filler = stash.profile(mk(9, "Depth RB", "RB", "EEE", 9), ctx)
    assert filler.bye_cover > 0
    assert filler.bye_gaps[0]["week"] == 7


def test_rival_credit_is_capped():
    needy = [[mk(50 + i, "Bad RB", "RB", "ZZZ", 1, slot=2)] for i in range(20)]
    ctx = ctx_for(base_roster(), others=needy)
    found = stash.profile(mk(9, "Depth RB", "RB", "EEE", 12), ctx)
    assert found.rivals_gaining == 20
    assert found.rival_credit <= stash.RIVAL_CREDIT_CAP


def test_stash_bids_are_small_and_capped():
    assert stash.stash_bid(0.5, 100, "RB")["dollars"] == 0
    assert stash.stash_bid(500, 100, "RB")["percent"] <= 8.0
    assert stash.stash_bid(500, 100, "K")["percent"] <= 3.0
    assert stash.stash_bid(10, 0, "RB")["dollars"] == 0


def test_scoring_label_shows_ppr_alongside_the_matchup_format():
    payload = {"settings": {"scoringSettings": {
        "scoringType": "H2H_POINTS", "scoringItems": [{"statId": 53, "points": 1.0}]}}}
    assert parse_settings(payload).scoring_type == "H2H_POINTS · PPR"


def test_stash_endpoint(http):
    body = http.get("/api/waivers/stash").json()
    assert "stashes" in body and body["weeks_left"] >= 1
    for row in body["stashes"]:
        assert row["bid_percent"] <= 8.0 and 1 <= row["priority"] <= 5
