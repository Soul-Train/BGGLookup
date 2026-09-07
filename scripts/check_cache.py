#!/usr/bin/env python3
"""Refuse to publish a bad cache.

The failure that actually hurts is not a crash, it is a build that half worked
and quietly shipped 400 games. The app would look fine and just stop
recognising things. So the build has to clear a floor before it goes out.
"""

import json
import os
import subprocess
import sys

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MIN_GAMES = 1000            # absolute floor
MAX_SHRINK = 0.10           # a 10% drop against the last commit is suspicious
MAX_BYTES = 8 * 1024 * 1024


def previous_count():
    """Count from the last committed cache, if there is one."""
    try:
        blob = subprocess.run(
            ["git", "show", "HEAD:data/meta.json"],
            capture_output=True, text=True, check=True, cwd=os.path.dirname(DATA),
        ).stdout
        return json.loads(blob).get("count", 0)
    except (subprocess.CalledProcessError, ValueError, OSError):
        return 0


def main():
    problems = []

    with open(os.path.join(DATA, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    with open(os.path.join(DATA, "games.min.json"), encoding="utf-8") as f:
        slim = json.load(f)

    games = slim["g"]
    if len(games) != meta["count"]:
        problems.append(f"meta says {meta['count']} games, file has {len(games)}")
    if len(games) < MIN_GAMES:
        problems.append(f"only {len(games)} games, floor is {MIN_GAMES}")
    if meta["bytes"] > MAX_BYTES:
        problems.append(f"{meta['bytes']} bytes is too big to ship to a phone")

    prev = previous_count()
    if prev and len(games) < prev * (1 - MAX_SHRINK):
        problems.append(f"shrank from {prev} to {len(games)}, more than {MAX_SHRINK:.0%}")

    missing = [g for g in games if not g.get("n") or not g.get("k") or not g.get("g")]
    if missing:
        problems.append(f"{len(missing)} records missing a name, key or rating")

    keys = {}
    for g in games:
        keys.setdefault(g["k"], 0)
        keys[g["k"]] += 1
    # Count affected records, not distinct keys. One key shared by 500 games is
    # 500 games the app cannot tell apart, not a single problem.
    collisions = sum(v for v in keys.values() if v > 1)
    if collisions > len(games) * 0.02:
        problems.append(f"{collisions} records share a match key, over the 2% tolerance")

    # Only meaningful once every record has a rating, so skip it if some do not.
    ratings = [g["g"] for g in games if g.get("g") is not None]
    if not missing and ratings and not all(
            ratings[i] >= ratings[i + 1] for i in range(len(ratings) - 1)):
        problems.append("games are not sorted by rating descending")

    if problems:
        print("Cache rejected:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"Cache looks good: {len(games)} games, "
          f"{meta['bytes']/1024:.0f} KB, {collisions} key collisions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
