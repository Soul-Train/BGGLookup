"""Thin, polite client for the BoardGameGeek XML API2.

BGG has three behaviours that will bite you if you ignore them:
  1. It answers 202 Accepted while it builds a response. You retry, you do not fail.
  2. It rate limits with 429 and expects you to back off.
  3. It has no CORS headers, so this has to run server side. Hence this script.
"""

import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://boardgamegeek.com/xmlapi2"
BROWSE = "https://boardgamegeek.com/browse/boardgame"
UA = "shelfworthy-cache/1.0 (+https://github.com/<you>/shelfworthy)"

# BGG asks for roughly one request every couple of seconds. Being greedy here
# gets you throttled for the whole run, which is slower than just waiting.
POLITE_DELAY = 2.0
MAX_TRIES = 6


class BggError(RuntimeError):
    pass


def fetch(url, tries=MAX_TRIES):
    """GET a URL, honouring BGG's 202-queued and 429-slow-down responses."""
    delay = POLITE_DELAY
    last = None
    for attempt in range(1, tries + 1):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=60) as r:
                body = r.read()
                if r.status == 202:
                    # Queued. BGG is building it. Wait and ask again.
                    last = "202 queued"
                    time.sleep(delay)
                    delay = min(delay * 1.8, 30)
                    continue
                return body
        except HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code in (202, 429, 500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 1.8, 60)
                continue
            raise BggError(f"{url} -> {last}") from e
        except URLError as e:
            last = str(e.reason)
            time.sleep(delay)
            delay = min(delay * 1.8, 60)
    raise BggError(f"{url} gave up after {tries} tries (last: {last})")


# ---------------------------------------------------------------- ranked ids

_ID_RE = re.compile(r"/boardgame(?:expansion)?/(\d+)/")


def ranked_ids(want, kind="boardgame"):
    """Game ids in BGG rank order.

    The XML API has no 'give me the top N' call, so this reads the same browse
    pages a person would, 100 ranks at a time, and pulls the ids out.
    """
    ids, page = [], 1
    seen = set()
    while len(ids) < want:
        url = f"{BROWSE}?sort=rank&rankobjecttype=subtype&rankobjectid=1&page={page}"
        if kind == "boardgameexpansion":
            url = f"{BROWSE}/expansion?sort=rank&page={page}"
        html = fetch(url).decode("utf-8", "replace")
        found = [int(m) for m in _ID_RE.findall(html)]
        fresh = [i for i in found if i not in seen]
        if not fresh:
            break  # ran out of pages
        for i in fresh:
            seen.add(i)
            ids.append(i)
        page += 1
        time.sleep(POLITE_DELAY)
    return ids[:want]


# ---------------------------------------------------------------- game detail

def _f(node, path, attr="value", cast=float, default=None):
    el = node.find(path)
    if el is None or el.get(attr) in (None, "", "Not Ranked"):
        return default
    try:
        return cast(el.get(attr))
    except (TypeError, ValueError):
        return default


def normalize(name):
    """Matching key: case, accents and punctuation folded away.

    'Brass: Birmingham' and 'brass birmingham' have to land in the same bucket,
    because nobody types a colon in a store aisle.
    """
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _latin(s):
    return all(ord(c) < 0x250 for c in s)


def parse_things(xml_bytes):
    """Turn a thing?stats=1 response into our cache records."""
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.findall("item"):
        primary = item.find("name[@type='primary']")
        if primary is None:
            continue
        name = primary.get("value")

        alts = []
        for n in item.findall("name[@type='alternate']"):
            v = n.get("value") or ""
            if v and _latin(v) and normalize(v) != normalize(name):
                alts.append(v)

        ratings = item.find("statistics/ratings")
        geek = avg = wt = None
        users = 0
        rank = None
        if ratings is not None:
            avg = _f(ratings, "average")
            geek = _f(ratings, "bayesaverage")
            wt = _f(ratings, "averageweight")
            users = int(_f(ratings, "usersrated", cast=float, default=0) or 0)
            for r in ratings.findall("ranks/rank"):
                if r.get("name") in ("boardgame", "rpgitem"):
                    try:
                        rank = int(r.get("value"))
                    except (TypeError, ValueError):
                        rank = None

        rec = {
            "id": int(item.get("id")),
            "n": name,
            "k": normalize(name),
            "alt": [normalize(a) for a in alts[:4]],
            "y": int(_f(item, "yearpublished", cast=float, default=0) or 0),
            "geek": round(geek, 3) if geek else None,
            "avg": round(avg, 3) if avg else None,
            "wt": round(wt, 2) if wt else None,
            "users": users,
            "rank": rank,
            "min": int(_f(item, "minplayers", cast=float, default=0) or 0),
            "max": int(_f(item, "maxplayers", cast=float, default=0) or 0),
            "time": int(_f(item, "playingtime", cast=float, default=0) or 0),
            "exp": item.get("type") == "boardgameexpansion",
            "img": (item.findtext("thumbnail") or "").strip() or None,
        }
        # A game with no rating is noise in a ranked list.
        if rec["geek"]:
            out.append(rec)
    return out


def things(ids, batch=100, progress=None):
    """Fetch full detail for many ids.

    BGG accepts large comma-separated id lists. Batches in the hundreds work;
    people doing bulk extraction report the ceiling is somewhere near a
    thousand, above which the server refuses outright. 100 is a compromise:
    few enough requests that a full build takes minutes rather than an hour,
    small enough that one transient failure does not cost much work.
    """
    got = []
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        url = f"{API}/thing?id={','.join(map(str, chunk))}&stats=1&type=boardgame,boardgameexpansion"
        try:
            got.extend(parse_things(fetch(url)))
        except (BggError, ET.ParseError):
            # A big batch failing costs 100 games. Retry it in halves rather
            # than losing the lot, since these errors are usually transient.
            if len(chunk) == 1:
                raise
            mid = len(chunk) // 2
            got.extend(things(chunk[:mid], batch=mid))
            got.extend(things(chunk[mid:], batch=len(chunk) - mid))
        if progress:
            progress(min(i + batch, len(ids)), len(ids))
        time.sleep(POLITE_DELAY)
    return got
