#!/usr/bin/env python3
"""Build the static game cache the app downloads.

Output (all under data/):
  games.json      full records, pretty-ish, for debugging
  games.min.json  what the app actually fetches
  meta.json       counts and build time, so the app can show cache age

The app never calls BGG directly. It downloads games.min.json once, keeps it in
IndexedDB, and only goes to the network for something not in the file.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bgg  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def load_previous(path):
    """Last good build, used as a fallback if this run partly fails."""
    try:
        with open(path, encoding="utf-8") as f:
            return {g["id"]: g for g in json.load(f)["games"]}
    except (OSError, ValueError, KeyError):
        return {}


def build(n_games, n_exp, min_users):
    prev = load_previous(os.path.join(OUT, "games.json"))
    log(f"previous build had {len(prev)} games")

    log(f"collecting top {n_games} ranked games")
    ids = bgg.ranked_ids(n_games, "boardgame")
    log(f"got {len(ids)} game ids")

    if n_exp:
        log(f"collecting top {n_exp} ranked expansions")
        ids += bgg.ranked_ids(n_exp, "boardgameexpansion")
        log(f"{len(ids)} ids total")

    def tick(done, total):
        if done % 200 == 0 or done == total:
            log(f"  detail {done}/{total}")

    try:
        games = bgg.things(ids, progress=tick)
    except bgg.BggError as e:
        if not prev:
            raise
        log(f"detail pass failed ({e}); keeping the previous build")
        games = list(prev.values())

    # Merge anything BGG dropped this run rather than losing it.
    merged = {g["id"]: g for g in games}
    restored = 0
    for gid, old in prev.items():
        if gid not in merged:
            merged[gid] = old
            restored += 1
    if restored:
        log(f"carried over {restored} records missing from this run")

    games = [g for g in merged.values() if (g.get("users") or 0) >= min_users]
    games.sort(key=lambda g: (-(g["geek"] or 0), g["n"]))
    log(f"{len(games)} games after the {min_users}-rating floor")

    # Warn about collisions, since two games sharing a match key means the app
    # cannot tell them apart from a photo.
    keys = {}
    for g in games:
        keys.setdefault(g["k"], []).append(g["n"])
    dupes = {k: v for k, v in keys.items() if len(v) > 1}
    if dupes:
        log(f"warning: {len(dupes)} duplicate match keys, e.g. {list(dupes.items())[:3]}")

    return games, dupes


def slim(g):
    """Only the fields the app renders or matches on."""
    return {
        "i": g["id"], "n": g["n"], "k": g["k"], "a": g["alt"],
        "y": g["y"], "g": g["geek"], "v": g["avg"], "w": g["wt"],
        "p": [g["min"], g["max"]], "t": g["time"],
        "x": 1 if g["exp"] else 0, "r": g["rank"],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=5000)
    ap.add_argument("--expansions", type=int, default=1000)
    ap.add_argument("--min-users", type=int, default=30,
                    help="drop games with fewer ratings than this")
    a = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    started = time.time()
    games, dupes = build(a.games, a.expansions, a.min_users)

    built = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(os.path.join(OUT, "games.json"), "w", encoding="utf-8") as f:
        json.dump({"built": built, "games": games}, f, ensure_ascii=False)
    with open(os.path.join(OUT, "games.min.json"), "w", encoding="utf-8") as f:
        json.dump({"b": built, "g": [slim(g) for g in games]},
                  f, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(os.path.join(OUT, "games.min.json"))
    meta = {
        "built": built,
        "count": len(games),
        "expansions": sum(1 for g in games if g["exp"]),
        "duplicate_keys": len(dupes),
        "bytes": size,
        "seconds": round(time.time() - started),
    }
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log(f"wrote {len(games)} games, {size/1024:.0f} KB, in {meta['seconds']}s")


if __name__ == "__main__":
    main()
