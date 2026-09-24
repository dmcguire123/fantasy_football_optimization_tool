"""
The breaking-news watcher: classifying stories, finding who benefits,
sizing the opening, and suggesting (never making) the claim.
"""

import httpx
import pytest
from conftest import make_player

from ffopt import constants as C
from ffopt import news
from ffopt.models import Player


# ------------------------------------------------------------ classify


def player(name="Star Back", position="RB", team="KC", owned=90.0, proj=18.0,
           availability=C.STATUS_ONTEAM, player_id=900):
    return Player(player_id=player_id, name=name, position=position, pro_team=team,
                  percent_owned=owned, projected_points=proj, espn_projected_points=proj,
                  availability=availability)


@pytest.mark.parametrize("headline,expected", [
    ("Back (knee) was placed on injured reserve Tuesday.", ("out", "ir")),
    ("Back suffered a season-ending torn ACL on Sunday.", ("out", "season")),
    ("Back (ankle) was ruled out for Sunday's game.", ("out", "week")),
    ("Back is expected to miss several weeks with a high-ankle sprain.", ("out", "multi")),
    ("Back was released by the Chiefs on Monday.", ("out", "season")),
    ("Back is expected to start Sunday against Denver.", ("role", None)),
    ("Back will take over as the lead back.", ("role", None)),
    ("Back (hip) was limited at practice Wednesday.", (None, None)),
])
def test_headlines_are_classified(headline, expected):
    assert news.classify(headline, player(name="Star Back")) == expected


def test_an_absence_named_after_a_teammate_is_the_teammates():
    headline = "Mariota will start with Daniels ruled out."
    names = {"mariota", "daniels"}
    # On Mariota's item this is a new role, not an absence.
    assert news.classify(headline, player(name="Marcus Mariota"), names) == ("role", None)
    # On Daniels's item it is his absence.
    assert news.classify(headline, player(name="Jayden Daniels"), names) == ("out", "week")


def test_news_must_be_about_the_player():
    assert news.classify("Kelce was ruled out.", player(name="Star Back")) == (None, None)
    assert news.mentions(player(name="Kenneth Walker III"), "walker was ruled out")


def test_missed_weeks_by_how_the_story_put_it():
    assert news.missed_weeks("season", 12) == 12
    assert news.missed_weeks("ir", 12) == 4
    assert news.missed_weeks("multi", 2) == 2
    assert news.missed_weeks("week", 12) == 1


# ------------------------------------------------------- beneficiaries


def test_an_absence_benefits_available_teammates_at_his_position():
    star = player()
    watch = [
        star,
        player("Next Man", proj=6.0, owned=5.0, availability=C.STATUS_FREEAGENT, player_id=901),
        player("Third Man", proj=3.0, owned=1.0, availability=C.STATUS_WAIVERS, player_id=902),
        player("Rostered Man", proj=9.0, owned=40.0, player_id=903),
        player("Other Team", team="DEN", proj=8.0, availability=C.STATUS_FREEAGENT, player_id=904),
        player("Receiver", position="WR", proj=8.0, availability=C.STATUS_FREEAGENT, player_id=905),
    ]
    signal = news.Signal("out", star, "Back was ruled out.", "week", "n1")
    assert [p.name for p in news.beneficiaries(signal, watch)] == ["Next Man", "Third Man"]


def test_nobody_benefits_from_an_irrelevant_players_absence():
    fringe = player(owned=3.0, proj=2.0)
    signal = news.Signal("out", fringe, "ruled out", "week", "n1")
    mate = player("Next Man", availability=C.STATUS_FREEAGENT, player_id=901)
    assert news.beneficiaries(signal, [fringe, mate]) == []


def test_a_new_role_benefits_the_player_himself_if_available():
    free = player("New Starter", availability=C.STATUS_FREEAGENT)
    taken = player("Taken Starter")
    assert news.beneficiaries(news.Signal("role", free, "will start"), [free]) == [free]
    assert news.beneficiaries(news.Signal("role", taken, "will start"), [taken]) == []


def test_stand_in_credits_a_share_of_the_starters_typical_week():
    star = player(proj=0.0)
    star.season_projected_points = 17 * 20.0
    backup = player("Next Man", proj=5.0, availability=C.STATUS_FREEAGENT, player_id=901)
    opening = news.stand_in(backup, news.Signal("out", star, "", "ir"), weeks_left=10)
    assert opening.projected_points == pytest.approx(0.6 * 20.0)
    assert opening.ros_points == pytest.approx((12.0 - 5.0) * 4)


def test_rushes_need_a_jump_over_the_players_own_pace(monkeypatch):
    counts = {1: {"a": 6000, "b": 9000, "c": 500}, 6: {"a": 12000, "b": 60000, "c": 600}}
    monkeypatch.setattr(news, "fetch_trending", lambda http=None, hours=1: counts[hours])
    # a: 6000 against 2000 an hour: a rush. b: 9000 against 10000: normal.
    # c: too few adds to matter.
    assert set(news.rushes()) == {"a"}


# ---------------------------------------------------------------- scan


def watch_records(last_news=1_000, star_status="ACTIVE"):
    star = make_player(901, "Star Back", "RB", 18.0, pro_team_id=12, percent_owned=90.0,
                       injury=star_status)
    star["lastNewsDate"] = last_news
    backup = make_player(902, "Next Man", "RB", 5.0, pro_team_id=12, percent_owned=4.0)
    backup["lastNewsDate"] = 1_000
    return [
        {"id": 901, "status": "ONTEAM", "onTeamId": 2, "player": star},
        {"id": 902, "status": "FREEAGENT", "player": backup},
    ]


def news_http(items):
    def handler(request):
        if "news/players" in str(request.url):
            return httpx.Response(200, json={"feed": items})
        return httpx.Response(200, json=[])
    return httpx.Client(transport=httpx.MockTransport(handler))


def scanner(service, tmp_path, records, items=()):
    service.client.fetch_free_agents = lambda week, limit=150, slot_ids=None, statuses=None: records
    return news.Scanner(service, news.open_db(tmp_path / "news.db"), http=news_http(list(items)),
                        notify=True, email_to="me@example.com", now=1_790_000_000.0)


@pytest.fixture
def quiet_alerts(monkeypatch):
    sent = {"mac": [], "email": []}
    monkeypatch.setattr(news, "notify_mac", lambda title, message: sent["mac"].append(title))
    monkeypatch.setattr(news, "notify_email",
                        lambda to, subject, body, html=None: sent["email"].append((to, subject, body, html)))
    return sent


IR_ITEM = {"id": "n1", "type": "Rotowire", "published": "2026-09-24T12:00:00Z",
           "headline": "Back (knee) was placed on injured reserve Tuesday.", "story": ""}


def test_first_scan_only_records_a_baseline(service, tmp_path, quiet_alerts):
    scan = scanner(service, tmp_path, watch_records())
    assert scan.run()["events"] == []
    assert quiet_alerts["email"] == []


def test_new_news_on_a_starter_opens_a_spot_for_his_backup(service, tmp_path, quiet_alerts):
    scan = scanner(service, tmp_path, watch_records(last_news=1_000))
    scan.run()
    scan = scanner(service, tmp_path, watch_records(last_news=2_000_000_000_000),
                      items=[IR_ITEM, {**IR_ITEM, "id": "n2", "type": "Story"}])
    result = scan.run()

    assert [e["candidate_name"] for e in result["events"]] == ["Next Man"]
    event = result["events"][0]
    assert event["kind"] == "out" and event["subject_name"] == "Star Back"
    assert event["projection"] == pytest.approx(0.6 * 18.0)
    assert event["action"] in ("suggested", "alerted")

    # One email for the scan, and it only suggests: nothing was claimed.
    assert len(quiet_alerts["email"]) == 1
    to, subject, body, html = quiet_alerts["email"][0]
    assert subject.endswith("Next Man (RB, KC)")
    assert "Nothing has been done for you" in body
    assert "python -m ffopt claim --add 902" in body
    assert "Next Man" in html and "python -m ffopt claim --add 902" in html


def test_the_same_story_is_only_reported_once(service, tmp_path, quiet_alerts):
    scan = scanner(service, tmp_path, watch_records(last_news=1_000))
    scan.run()
    later = watch_records(last_news=2_000_000_000_000)
    scan = scanner(service, tmp_path, later, items=[IR_ITEM])
    assert len(scan.run()["events"]) == 1
    scan = scanner(service, tmp_path, later, items=[IR_ITEM])
    assert scan.run()["events"] == []


def test_an_injury_status_change_is_news_too(service, tmp_path, quiet_alerts):
    scan = scanner(service, tmp_path, watch_records())
    scan.run()
    scan = scanner(service, tmp_path, watch_records(star_status="OUT"))
    events = scan.run()["events"]
    assert [e["candidate_name"] for e in events] == ["Next Man"]
    assert "Out" in events[0]["headline"]


def test_the_watcher_never_submits_a_transaction(service, tmp_path, quiet_alerts, requests_log):
    scan = scanner(service, tmp_path, watch_records(last_news=1_000))
    scan.run()
    scan = scanner(service, tmp_path, watch_records(last_news=2_000_000_000_000),
                      items=[IR_ITEM])
    scan.run()
    assert not [r for r in requests_log if r.method == "POST"]


def test_claim_instructions_spell_out_the_move():
    event = {"candidate_availability": C.STATUS_WAIVERS, "candidate_id": 902, "drop_id": 7,
             "candidate_name": "Next Man", "drop_name": "Old Guy", "bid": 12.0,
             "action": "suggested"}
    text = news.claim_instructions(event)
    assert "Suggested claim: Add Next Man, drop Old Guy, bid $12 (waiver claim)" in text
    assert "python -m ffopt claim --add 902 --drop 7 --bid 12" in text

    event.update(candidate_availability=C.STATUS_FREEAGENT, drop_id=None, drop_name=None,
                 action="alerted")
    text = news.claim_instructions(event)
    assert "--free-agent" in text and "If you want him" in text


def email_events():
    base = {"candidate_position": "RB", "candidate_team": "KC", "projection": 10.8,
            "headline": "Star (knee) was placed on injured reserve <Tuesday> & more.",
            "note": "", "drop_id": 7, "drop_name": "Old Guy", "bid": 0.0}
    return [
        {**base, "action": "suggested", "candidate_name": "Next Man", "candidate_id": 902,
         "candidate_availability": C.STATUS_FREEAGENT, "weekly_gain": 3.4, "season_gain": 18.0},
        {**base, "action": "alerted", "candidate_name": "Third Man", "candidate_id": 903,
         "candidate_availability": C.STATUS_WAIVERS, "bid": 9.0, "weekly_gain": 0.0,
         "season_gain": 0.0, "note": "Starter-level for anyone."},
    ]


def test_email_subject_leads_with_the_strongest_opening():
    assert news.email_subject(email_events()) == "Claim now: Next Man (RB, KC) + 1 more"
    assert news.email_subject(email_events()[1:]) == "Pickup alert: Third Man (RB, KC)"


def test_email_text_and_html_carry_every_opening():
    events = email_events()
    text = news.email_text(events)
    assert text.startswith("PICKUP ALERT: 1 suggested claim, 1 worth a look")
    assert "Move: Add Next Man, drop Old Guy (free agent: first come, first served)" in text
    assert "Move: Add Third Man, drop Old Guy, bid $9 (waiver claim)" in text
    assert "Note: Starter-level for anyone." in text
    assert "*" not in text and "#" not in text

    html = news.email_html(events)
    assert "SUGGESTED CLAIM" in html and "WORTH A LOOK" in html
    # Headlines are escaped, never injected as markup.
    assert "&lt;Tuesday&gt; &amp; more" in html and "<Tuesday>" not in html
    assert "python -m ffopt claim --add 903 --drop 7 --bid 9" in html
