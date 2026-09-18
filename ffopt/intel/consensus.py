"""Merge scraped articles and trending adds into one ranked list."""

from . import matching


# Points toward the consensus score. An article naming a player is worth more
# than one extra mention in the same article, and every distinct source counts.
ARTICLE_POINTS = 2.0
TRENDING_MAX_POINTS = 3.0
MAX_NOTES = 3


# Build the ranked consensus for the pool of available players.
def build_consensus(pool, articles, trending):
    index = matching.build_index(pool)
    entries = {}

    def entry_for(player):
        if player.player_id not in entries:
            entries[player.player_id] = {
                "player": player,
                "sources": set(),
                "articles": [],
                "notes": [],
                "trending_rank": None,
                "trending_adds": 0,
            }
        return entries[player.player_id]

    for article in articles:
        for player, snippet in matching.find_mentions(article.text, index):
            entry = entry_for(player)
            entry["sources"].add(article.source)
            entry["articles"].append({"source": article.source, "title": article.title, "url": article.url})
            if len(entry["notes"]) < MAX_NOTES:
                entry["notes"].append({"source": article.source, "text": snippet})

    for add in trending:
        key = matching.normalize_name(add.name)
        player = index.get(key)
        if not player:
            continue
        entry = entry_for(player)
        entry["sources"].add("Sleeper trending")
        entry["trending_rank"] = add.rank
        entry["trending_adds"] = add.count

    ranked = []
    for entry in entries.values():
        score = 0.0
        distinct_articles = {a["url"] for a in entry["articles"]}
        source_names = {a["source"] for a in entry["articles"]}
        score += ARTICLE_POINTS * len(source_names)
        score += 0.5 * max(0, len(distinct_articles) - len(source_names))
        if entry["trending_rank"]:
            score += TRENDING_MAX_POINTS * max(0.0, 1.0 - (entry["trending_rank"] - 1) / 100.0)

        player = entry["player"]
        ranked.append(
            {
                "player": player.to_dict(),
                "player_id": player.player_id,
                "score": round(score, 2),
                "source_count": len(entry["sources"]),
                "sources": sorted(entry["sources"]),
                "articles": entry["articles"][:6],
                "notes": entry["notes"],
                "trending_rank": entry["trending_rank"],
                "trending_adds": entry["trending_adds"],
            }
        )

    ranked.sort(key=lambda e: (e["score"], e["player"].get("percent_owned", 0)), reverse=True)
    for position, entry in enumerate(ranked, start=1):
        entry["rank"] = position
    return ranked
