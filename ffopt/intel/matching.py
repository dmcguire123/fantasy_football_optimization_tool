"""Find players from the available pool inside scraped text."""

import re


SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


# Lowercase, drop punctuation and generational suffixes so that
# "Chris Godwin Jr." and "Chris Godwin" compare equal.
def normalize_name(name):
    cleaned = re.sub(r"[^a-z\s]", "", (name or "").lower().replace("'", ""))
    parts = [p for p in cleaned.split() if p not in SUFFIXES]
    return " ".join(parts)


# Map normalized full name to Player for the available pool. D/ST units are
# left out because team names match far too much ordinary text.
def build_index(players):
    index = {}
    for player in players:
        if player.position.upper() in ("D/ST", "DST"):
            continue
        key = normalize_name(player.name)
        if key and " " in key:
            index[key] = player
    return index


# Return (player, snippet) for every pool player named in the text. Only full
# names match, so a common surname never lights up the wrong player.
def find_mentions(text, index, radius=220):
    lowered = normalize_name_text(text)
    found = []
    for key, player in index.items():
        hit = re.search(r"\b" + re.escape(key) + r"\b", lowered)
        if not hit:
            continue
        position = hit.start()
        start = max(0, position - radius)
        end = min(len(lowered), position + len(key) + radius)
        found.append((player, snippet_from(text, key, start, end)))
    return found


# The lowered text keeps character positions aligned with the original for
# plain names, so lookups on it are safe. Punctuation is only stripped from
# names, never from the text being searched, apart from apostrophes.
def normalize_name_text(text):
    return re.sub(r"[^a-z\s]", " ", (text or "").lower().replace("'", ""))


def snippet_from(text, key, start, end):
    return re.sub(r"\s+", " ", (text or "")[start:end]).strip()
