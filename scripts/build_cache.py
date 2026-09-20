#!/usr/bin/env python3
"""Build the Shelfworthy game cache from BoardGameGeek.

Runs weekly in GitHub Actions. Writes data/games.json, which the app downloads
once and keeps on the phone. Anything not in the file still works: the app
falls back to a live lookup.

Needs BGG_TOKEN in the environment (a GitHub Actions secret).
"""

import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import subprocess
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://boardgamegeek.com/xmlapi2"
CSV_URL = "https://boardgamegeek.com/data_dumps/bg_ranks"
UA = "shelfworthy-cache/2.0"
TOKEN = os.environ.get("BGG_TOKEN", "").strip()

DELAY = 1.0          # BGG is slow on purpose; being greedy gets you throttled
BATCH = 40           # small batches: a rejection costs less to split
BUDGET_MIN = float(os.environ.get("BUDGET_MIN", "90"))   # stop and save by then
CEILING = int(os.environ.get("CEILING", "460000"))       # top of BGG's id space
NEW_OVERLAP = 2000   # re-check a little below the frontier, in case of gaps
CHECKPOINT_MIN = float(os.environ.get("CHECKPOINT_MIN", "10"))  # save this often
MAX_TRIES = 5
# Below this, a game is too obscure to be sitting on a shop shelf, and every
# one of them costs space in the file your phone downloads.
MIN_RATINGS = int(os.environ.get("MIN_RATINGS", "100"))

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "sweep_state.json")


class BadBatch(Exception):
    """BGG refused this particular set of ids."""


def log(m):
    print(m, flush=True)


def get(url, accept="application/xml"):
    """GET with the token, honouring BGG's queued and throttled responses."""
    delay, last = DELAY, None
    for _ in range(MAX_TRIES):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept": accept,
                "Authorization": "Bearer " + TOKEN,
            })
            with urlopen(req, timeout=120) as r:
                if r.status == 202:          # queued, ask again
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                return r.read()
        except HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (401, 403):
                raise SystemExit(
                    "BGG rejected the token (%d). Check the BGG_TOKEN secret." % e.code)
            if e.code == 400:
                # BGG rejects a batch outright if it dislikes any id in it.
                # Caller splits and retries rather than losing the whole run.
                raise BadBatch("400 on %d ids" % url.count(",") if "," in url else "400")
            if e.code in (202, 429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
        except URLError as e:
            last = str(e.reason)
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise RuntimeError("gave up on %s (%s)" % (url, last))


def norm(name):
    """Matching key: case, accents and punctuation folded away.

    'Brass: Birmingham' and 'brass birmingham' land in the same bucket,
    because nobody types a colon while standing in a shop aisle.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- id sources

def ids_from_csv(limit):
    """BGG publishes a ranks CSV to approved applications. Cheapest source."""
    raw = get(CSV_URL, accept="text/csv")
    text = raw.decode("utf-8", "replace")
    if "<html" in text[:300].lower():
        raise ValueError("got a web page, not a CSV")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("CSV was empty")
    key_id = next(k for k in rows[0] if k.lower() in ("id", "objectid"))
    key_rank = next((k for k in rows[0] if "rank" in k.lower()), None)

    def rank_of(r):
        try:
            v = int(r.get(key_rank) or 0)
            return v if v > 0 else 10 ** 9
        except (TypeError, ValueError):
            return 10 ** 9

    rows.sort(key=rank_of)
    return [int(r[key_id]) for r in rows[:limit] if str(r.get(key_id, "")).isdigit()]


def read_state():
    try:
        with open(STATE) as f:
            s = json.load(f)
        return {"next": int(s.get("next", 1)),
                "frontier": int(s.get("frontier", 0)),
                "filled": bool(s.get("filled", False))}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"next": 1, "frontier": 0, "filled": False}


def save_progress(next_id, frontier=None, filled=None):
    os.makedirs(DATA, exist_ok=True)
    s = read_state()
    s["next"] = next_id
    if frontier is not None:
        s["frontier"] = frontier
    if filled is not None:
        s["filled"] = filled
    with open(STATE, "w") as f:
        json.dump(s, f)


def plan_run():
    """Decide what this run covers.

    While filling, walk upward from where the last run stopped. Once the whole
    id space has been covered, start each run at the frontier instead, because
    the newest games always get the highest ids. New releases then show up
    within a week rather than whenever a refresh pass happens to reach them.
    """
    s = read_state()

    if s["filled"]:
        start = max(s["frontier"] - NEW_OVERLAP, 1)
        log("refresh mode: checking for new ids from %d" % start)
        return {"start": start, "filled": True, "frontier": s["frontier"]}

    if s["next"] > CEILING:
        start = max(s["frontier"] - NEW_OVERLAP, 1)
        log("id space covered; switching to new-games-first from %d" % start)
        return {"start": start, "filled": True, "frontier": s["frontier"]}

    return {"start": s["next"], "filled": False, "frontier": s["frontier"]}


# ---------------------------------------------------------------- details

def num(node, path, attr="value"):
    e = node.find(path)
    if e is None:
        return None
    try:
        return float(e.get(attr))
    except (TypeError, ValueError):
        return None


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.findall("item"):
        pn = item.find('name[@type="primary"]')
        stats = item.find("statistics/ratings")
        if pn is None or stats is None:
            continue
        if item.get("type") not in ("boardgame", "boardgameexpansion"):
            continue                       # videogames, rpgs and the rest
        name = pn.get("value")
        users = num(stats, "usersrated") or 0
        geek = num(stats, "bayesaverage")
        if users < MIN_RATINGS or not geek:
            continue

        rank = None
        for r in stats.findall("ranks/rank"):
            if r.get("name") == "boardgame":
                try:
                    rank = int(r.get("value"))
                except (TypeError, ValueError):
                    rank = None

        alts = []
        for a in item.findall('name[@type="alternate"]'):
            v = a.get("value") or ""
            if v and all(ord(c) < 0x250 for c in v) and norm(v) != norm(name):
                alts.append(norm(v))

        out.append({
            "i": int(item.get("id")),
            "n": name,
            "k": norm(name),
            "a": alts[:3],
            "y": int(num(item, "yearpublished") or 0),
            "g": round(num(stats, "average") or 0, 2),
            "b": round(geek, 2),
            "w": round(num(stats, "averageweight") or 0, 2),
            "r": rank,
            "u": int(users),
            "x": 1 if item.get("type") == "boardgameexpansion" else 0,
        })
    return out


LIMIT = int(os.environ.get("LIMIT", "20000"))


def write_cache(games, reached, final=False):
    """Merge into the existing file and write it out. Safe to call repeatedly."""
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "games.json")
    merged = {}
    try:
        with open(path, encoding="utf-8") as f:
            for g in json.load(f)["g"]:
                merged[g["i"]] = g
    except (OSError, ValueError, KeyError):
        pass
    carried = len(merged)
    for g in games:
        merged[g["i"]] = g
    # Re-apply the floor to everything, so raising it prunes older entries too.
    merged = {i: g for i, g in merged.items() if (g.get("u") or 0) >= MIN_RATINGS}
    if not merged:
        if final:
            raise SystemExit("nothing fetched; refusing to publish an empty cache")
        return

    out = sorted(merged.values(), key=lambda g: (-(g["b"] or 0), g["n"]))[:LIMIT]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"built": time.strftime("%Y-%m-%d"), "g": out},
                  f, ensure_ascii=False, separators=(",", ":"))

    # What the phone actually downloads. The app uses the cache only to turn a
    # title into an id, so shipping ratings and weights to it is dead weight.
    idx = {}
    for g in out:
        idx.setdefault(g["k"], g["i"])
        for k in g.get("a", []):
            idx.setdefault(k, g["i"])
    ipath = os.path.join(DATA, "index.json")
    with open(ipath, "w", encoding="utf-8") as f:
        json.dump({"built": time.strftime("%Y-%m-%d"), "k": idx},
                  f, ensure_ascii=False, separators=(",", ":"))

    # A readable copy, for opening in Excel or Power BI without touching JSON.
    cpath = os.path.join(DATA, "games.csv")
    with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["BGG ID", "Name", "Year", "Average", "Geek Rating",
                    "Weight", "Rank", "Ratings", "Is Expansion", "BGG Link"])
        for g in out:
            w.writerow([g["i"], g["n"], g["y"] or "", g["g"] or "", g["b"] or "",
                        g["w"] or "", g["r"] or "", g["u"] or "",
                        "Yes" if g["x"] else "No",
                        "https://boardgamegeek.com/boardgame/%d" % g["i"]])

    save_progress(reached)
    size = os.path.getsize(path)
    isize = os.path.getsize(ipath)
    with open(os.path.join(DATA, "meta.json"), "w") as f:
        json.dump({"built": time.strftime("%Y-%m-%d %H:%M UTC"),
                   "count": len(out), "bytes": size,
                   "index_keys": len(idx), "index_bytes": isize,
                   "swept_to": reached, "carried_forward": carried}, f, indent=2)
    if final:
        log("wrote %d games (%d new), %.0f KB; index %d keys, %.0f KB"
            % (len(out), len(out) - carried, size / 1024, len(idx), isize / 1024))


REJECTS = {"n": 0}


def git_push(message):
    """Commit whatever is on disk right now.

    Without this, a run that is killed loses everything it gathered, because
    the workflow's own commit step never gets to run.
    """
    if os.environ.get("NO_GIT") == "1":
        return
    try:
        subprocess.run(["git", "config", "user.name", "cache-bot"], check=True, cwd=HERE)
        subprocess.run(["git", "config", "user.email",
                        "cache-bot@users.noreply.github.com"], check=True, cwd=HERE)
        subprocess.run(["git", "add", "data"], check=True, cwd=HERE)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE)
        if r.returncode == 0:
            return                      # nothing changed
        subprocess.run(["git", "commit", "-m", message], check=True, cwd=HERE)
        subprocess.run(["git", "push"], check=True, cwd=HERE)
        log("  checkpoint pushed: %s" % message)
    except (subprocess.CalledProcessError, OSError) as e:
        log("  checkpoint push failed (%s), carrying on" % e)


def sweep(start, deadline):
    """Walk ids upward until the clock runs out. Always returns what it has.

    The run is time-boxed rather than size-boxed, because how far it gets
    depends on how many batches BGG rejects, which we cannot predict.
    """
    got, at = [], start
    REJECTS["n"] = 0
    next_save = time.time() + CHECKPOINT_MIN * 60
    while time.time() < deadline:
        chunk = list(range(at, at + BATCH))
        got.extend(fetch_chunk(chunk))
        at += BATCH
        if REJECTS["n"] > 150 and not got:
            raise SystemExit(
                "BGG refused every batch (%d rejections, nothing fetched). "
                "The request shape is wrong, not the ids." % REJECTS["n"])
        if (at - start) % (BATCH * 25) == 0:
            left = int((deadline - time.time()) / 60)
            log("  id %d, %d games kept, %d batches split, %d min left"
                % (at, len(got), REJECTS["n"], left))
        if time.time() > next_save:
            write_cache(got, at)
            git_push("Cache checkpoint at id %d" % at)
            next_save = time.time() + CHECKPOINT_MIN * 60
        time.sleep(DELAY)
    log("time budget reached at id %d" % at)
    return got, at


def fetch_chunk(chunk, depth=0):
    """Fetch one batch, halving it if BGG rejects the set."""
    url = "%s/thing?stats=1&id=%s" % (API, ",".join(map(str, chunk)))
    try:
        return parse(get(url))
    except BadBatch:
        REJECTS["n"] += 1
        if len(chunk) == 1:
            return []              # that single id is simply not fetchable
        mid = len(chunk) // 2
        time.sleep(DELAY)
        return fetch_chunk(chunk[:mid], depth + 1) + fetch_chunk(chunk[mid:], depth + 1)
    except (ET.ParseError, RuntimeError) as e:
        log("  batch skipped (%s)" % e)
        return []


# ---------------------------------------------------------------- main

def main():
    if not TOKEN:
        raise SystemExit("No BGG_TOKEN set. Add it as a repository secret.")

    os.makedirs(DATA, exist_ok=True)

    deadline = time.time() + BUDGET_MIN * 60
    plan = plan_run()
    start = plan["start"]
    log("sweeping from id %d, budget %g minutes" % (start, BUDGET_MIN))

    try:
        games, reached = sweep(start, deadline)
    except KeyboardInterrupt:
        games, reached = [], start

    # The frontier is the highest id ever confirmed as a real game.
    frontier = max(plan["frontier"], max([g["i"] for g in games], default=0))
    save_progress(reached, frontier=frontier, filled=plan["filled"])
    log("%d games cleared the %d-rating floor; next run starts at %d"
        % (len(games), MIN_RATINGS, reached))

    write_cache(games, reached, final=True)
    save_progress(reached, frontier=frontier, filled=plan["filled"])
    log("frontier is id %d%s"
        % (frontier, "; in refresh mode" if plan["filled"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())