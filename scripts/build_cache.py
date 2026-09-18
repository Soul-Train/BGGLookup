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
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://boardgamegeek.com/xmlapi2"
CSV_URL = "https://boardgamegeek.com/data_dumps/bg_ranks"
UA = "shelfworthy-cache/2.0"
TOKEN = os.environ.get("BGG_TOKEN", "").strip()

DELAY = 1.5          # BGG is slow on purpose; being greedy gets you throttled
BATCH = 100          # ids per thing call
MAX_TRIES = 5
MIN_RATINGS = 30     # below this a rating means very little

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "sweep_state.json")


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


def ids_from_sweep(slice_size):
    """Fallback: walk the id space a slice at a time, resuming each week."""
    try:
        with open(STATE) as f:
            start = json.load(f).get("next", 1)
    except (OSError, ValueError):
        start = 1
    if start > 450000:
        start = 1                      # wrap round and refresh the whole space
    end = start + slice_size
    os.makedirs(DATA, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"next": end}, f)
    log("sweeping ids %d to %d" % (start, end))
    return list(range(start, end))


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


def fetch_details(ids):
    got, n = [], len(ids)
    for i in range(0, n, BATCH):
        chunk = ids[i:i + BATCH]
        url = "%s/thing?stats=1&type=boardgame,boardgameexpansion&id=%s" % (
            API, ",".join(map(str, chunk)))
        try:
            got.extend(parse(get(url)))
        except (ET.ParseError, RuntimeError) as e:
            log("  batch at %d failed (%s), skipping it" % (i, e))
        if (i // BATCH) % 10 == 0:
            log("  %d/%d ids checked, %d games kept" % (min(i + BATCH, n), n, len(got)))
        time.sleep(DELAY)
    return got


# ---------------------------------------------------------------- main

def main():
    if not TOKEN:
        raise SystemExit("No BGG_TOKEN set. Add it as a repository secret.")

    limit = int(os.environ.get("LIMIT", "6000"))
    os.makedirs(DATA, exist_ok=True)

    try:
        log("trying the ranks CSV")
        ids = ids_from_csv(limit)
        log("got %d ids from the CSV" % len(ids))
    except Exception as e:                      # noqa: BLE001 - any failure falls back
        log("CSV not usable (%s); falling back to an id sweep" % e)
        ids = ids_from_sweep(int(os.environ.get("SLICE", "60000")))

    games = fetch_details(ids)
    log("%d games cleared the %d-rating floor" % (len(games), MIN_RATINGS))

    # Merge with last week's file so a partial run never shrinks the cache.
    path = os.path.join(DATA, "games.json")
    merged = {}
    try:
        with open(path, encoding="utf-8") as f:
            for g in json.load(f)["g"]:
                merged[g["i"]] = g
        log("carried forward %d from last week" % len(merged))
    except (OSError, ValueError, KeyError):
        pass
    for g in games:
        merged[g["i"]] = g

    out = sorted(merged.values(), key=lambda g: (-(g["b"] or 0), g["n"]))[:limit]
    if len(out) < 200:
        raise SystemExit("only %d games; refusing to publish that" % len(out))

    with open(path, "w", encoding="utf-8") as f:
        json.dump({"built": time.strftime("%Y-%m-%d"), "g": out},
                  f, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(path)
    log("wrote %d games, %.0f KB" % (len(out), size / 1024))

    with open(os.path.join(DATA, "meta.json"), "w") as f:
        json.dump({"built": time.strftime("%Y-%m-%d %H:%M UTC"),
                   "count": len(out), "bytes": size}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
