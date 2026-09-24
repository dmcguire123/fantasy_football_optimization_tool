"""Ties scraping, consensus, lineup gain, bids and analysis together."""

import time

from .. import waivers
from .. import constants as C
from . import analysis, bids, consensus, sources, stash


# How many available players to pull, and how many the lineup solver scores.
POOL_SIZE = 300
BASE_EVALUATED = 40
MAX_TARGETS = 20


class IntelService:
    def __init__(self, league_service, transport=None, llm_client=None, feeds=None):
        self.league_service = league_service
        self.settings = league_service.settings
        self.transport = transport
        self.llm_client = llm_client
        self.feeds = feeds if feeds is not None else sources.FEEDS
        self._cache = {}

    def refresh(self):
        self._cache.clear()

    # Fetch and merge the web sources, reusing results for the intel TTL.
    def consensus(self, week=None):
        league = self.league_service.load_league(week)
        key = ("consensus", league.week)
        ttl = self.settings.intel_ttl_seconds
        hit = self._cache.get(key)
        if hit and ttl > 0 and (time.monotonic() - hit[0]) < ttl:
            return hit[1]

        pool = self.league_service.free_agents(week=league.week, limit=POOL_SIZE)
        client = sources.IntelClient(self.settings, transport=self.transport)
        try:
            articles, trending, statuses, fetched_at = sources.gather_all(client, feeds=self.feeds)
        finally:
            client.close()

        ranked = consensus.build_consensus(pool, articles, trending)
        value = {
            "week": league.week,
            "fetched_at": fetched_at,
            "pool_size": len(pool),
            "sources": [s.to_dict() for s in statuses],
            "players": ranked,
        }
        self._cache[key] = (time.monotonic(), value)
        return value

    # Every team's roster with what it is short on.
    def rosters(self, week=None):
        league = self.league_service.load_league(week)
        teams = []
        for team in league.teams:
            counts = team.position_counts()
            needs = [
                pos for pos, depth in bids.MIN_DEPTH.items()
                if counts.get(pos, 0) < depth
            ]
            data = team.to_dict()
            data["needs"] = needs
            teams.append(data)
        return {
            "week": league.week,
            "my_team_id": self.league_service.my_team(week).team_id,
            "teams": teams,
        }

    # The sheet for my team: who to want and what to bid.
    def targets(self, week=None, limit=MAX_TARGETS):
        league = self.league_service.load_league(week)
        team = self.league_service.my_team(week)
        settings = league.settings
        starting_slots = settings.starting_slots()
        weeks_left = max(1, settings.final_week - league.week + 1)

        intel = self.consensus(week)
        by_id = {e["player_id"]: e for e in intel["players"]}

        pool = self.league_service.free_agents(week=league.week, limit=POOL_SIZE)
        expert_ids = set(by_id)
        chosen = [p for p in pool if p.player_id in expert_ids]
        others = [p for p in pool if p.player_id not in expert_ids]
        others.sort(key=lambda p: p.effective_projection, reverse=True)
        chosen.extend(others[:BASE_EVALUATED])

        evaluations = waivers.recommend_pickups(
            team,
            chosen,
            starting_slots,
            roster_limit=settings.active_roster_size,
            faab_remaining=team.faab_remaining,
            weeks_left=weeks_left,
            limit=len(chosen),
            min_gain=0.0,
            evaluate_top=len(chosen),
        )

        rows = []
        for ev in evaluations:
            entry = by_id.get(ev.player.player_id)
            source_count = entry["source_count"] if entry else 0
            if not entry and ev.weekly_gain < 0.5:
                continue

            # A stream only pays off this week, so it is bid on as one week.
            advice = bids.bid_advice(
                weekly_gain=ev.weekly_gain,
                season_gain=ev.season_gain,
                faab_remaining=team.faab_remaining,
                weeks_left=1 if ev.move_type == "stream" else weeks_left,
                source_count=source_count,
                position=ev.player.position,
                league=league,
                my_team_id=team.team_id,
            )
            rows.append({"evaluation": ev, "entry": entry, "advice": advice})

        candidates = []
        for row in rows:
            ev, entry, advice = row["evaluation"], row["entry"], row["advice"]
            candidates.append(
                {
                    "player_id": ev.player.player_id,
                    "name": ev.player.name,
                    "position": ev.player.position,
                    "team": ev.player.pro_team,
                    "injury": ev.player.injury_status,
                    "weekly_gain": round(ev.weekly_gain, 2),
                    "season_gain": round(ev.season_gain, 2),
                    "drop": ev.drop_player.name if ev.drop_player else None,
                    "move_type": ev.move_type,
                    "note": ev.note,
                    "source_count": entry["source_count"] if entry else 0,
                    "consensus_score": entry["score"] if entry else 0.0,
                    "expert_notes": [n["text"] for n in entry["notes"]] if entry else [],
                    "suggested_bid_percent": advice["percent"],
                }
            )

        context = {
            "faab_remaining": team.faab_remaining,
            "weeks_left": weeks_left,
            "position_counts": team.position_counts(),
        }
        verdicts, mode, note = analysis.analyze(
            self.settings, candidates, context, client=self.llm_client
        )

        results = []
        for row, cand in zip(rows, candidates):
            ev, entry, advice = row["evaluation"], row["entry"], row["advice"]
            verdict = verdicts.get(cand["player_id"], {"priority": 1, "reasoning": "", "risk": ""})
            score = ev.net_gain + 0.4 * cand["consensus_score"] + 1.5 * (verdict["priority"] - 3)
            # Whatever wrote the reasoning, a stream's season cost is stated.
            reasoning = verdict["reasoning"]
            if ev.note and ev.note not in reasoning:
                reasoning = f"{reasoning} {ev.note}".strip()
            data = ev.to_dict()
            data.update(
                {
                    "bid_percent": advice["percent"],
                    "bid_dollars": advice["dollars"],
                    "bid_reasons": advice["reasons"],
                    "priority": verdict["priority"],
                    "reasoning": reasoning,
                    "risk": verdict["risk"],
                    "source_count": cand["source_count"],
                    "sources": entry["sources"] if entry else [],
                    "target_score": round(score, 2),
                }
            )
            results.append(data)

        results.sort(key=lambda r: r["target_score"], reverse=True)
        return {
            "week": league.week,
            "faab_remaining": team.faab_remaining,
            "weeks_left": weeks_left,
            "analysis": mode,
            "analysis_note": note,
            "sources": intel["sources"],
            "targets": results[:limit],
            "drop_candidates": waivers.drop_candidates(team, starting_slots),
        }

    # Bench stashes: players who insure your lineup even if they never start.
    def stash_picks(self, week=None, limit=MAX_TARGETS):
        league = self.league_service.load_league(week)
        team = self.league_service.my_team(week)
        settings = league.settings
        starting_slots = settings.starting_slots()

        bye_by_team = stash.bye_map_by_abbrev(self.league_service.bye_weeks())
        others = [t for t in league.teams if t.team_id != team.team_id]
        ctx = stash.build_context(
            team, others, starting_slots, league.week, settings.final_week, bye_by_team
        )

        intel = self.consensus(week)
        by_id = {e["player_id"]: e for e in intel["players"]}

        pool = self.league_service.free_agents(week=league.week, limit=POOL_SIZE)
        pool = [p for p in pool if p.position in stash.SKILL_POSITIONS]

        drops = waivers.drop_candidates(team, starting_slots)
        roster_full = bool(settings.active_roster_size) and (
            len(ctx.roster) >= settings.active_roster_size
        )

        rows = []
        for player in pool:
            found = stash.profile(player, ctx)
            if found.score < stash.STASH_LIST_MIN_SCORE:
                continue
            advice = stash.stash_bid(
                found.score, team.faab_remaining, player.position, found.rivals_gaining
            )
            rows.append({"player": player, "profile": found, "advice": advice})

        candidates = []
        for row in rows:
            p, f = row["player"], row["profile"]
            entry = by_id.get(p.player_id)
            candidates.append(
                {
                    "player_id": p.player_id,
                    "name": p.name,
                    "position": p.position,
                    "team": p.pro_team,
                    "stash_score": round(f.score, 2),
                    "injury_cover": round(f.injury_cover, 2),
                    "bye_cover": round(f.bye_cover, 2),
                    "handcuff_of": f.handcuff_of,
                    "rivals_gaining": f.rivals_gaining,
                    "reasons": f.reasons,
                    "source_count": entry["source_count"] if entry else 0,
                }
            )

        context = {
            "faab_remaining": team.faab_remaining,
            "weeks_left": ctx.weeks_left,
            "scoring": settings.scoring_type,
            "starters": [s.name for s in ctx.starters],
        }
        verdicts, mode, note = analysis.analyze(
            self.settings, candidates, context, client=self.llm_client, kind="stash"
        )

        results = []
        for row, cand in zip(rows, candidates):
            p, f, advice = row["player"], row["profile"], row["advice"]
            verdict = verdicts.get(p.player_id, {"priority": 1, "reasoning": "", "risk": ""})
            data = {
                "player": p.to_dict(),
                "priority": verdict["priority"],
                "reasoning": verdict["reasoning"],
                "risk": verdict["risk"],
                "bid_percent": advice["percent"],
                "bid_dollars": advice["dollars"],
                "source_count": cand["source_count"],
                "needs_drop": roster_full,
                "drop_player": drops[0]["player"] if drops and roster_full else None,
                "sort_key": f.score + 2.0 * (verdict["priority"] - 3),
            }
            data.update(f.to_dict())
            results.append(data)

        results.sort(key=lambda r: r["sort_key"], reverse=True)
        return {
            "week": league.week,
            "weeks_left": ctx.weeks_left,
            "scoring": settings.scoring_type,
            "faab_remaining": team.faab_remaining,
            "analysis": mode,
            "analysis_note": note,
            "stashes": results[:limit],
            "drop_candidates": drops,
        }
