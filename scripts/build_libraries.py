#!/usr/bin/env python3
"""Build data/libraries.json: every live Libby library, for the books app's library search.

Libby's library list can't be searched by name, so once a week this saves the whole
list (about 13,000 entries, most of them closed or inactive) and keeps the live ones.
The phone then searches the file locally.

No keys needed: this is the same public list the Libby app reads before you sign in.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = os.environ.get("LIBBY_API", "https://thunder.api.overdrive.com/v2/libraries")
UA = "shelfworthy-libraries/1.0 (personal book lookup)"
DELAY = float(os.environ.get("DELAY", "0.4"))   # be polite: this is an unofficial endpoint
MAX_PAGES = 1000
MAX_TRIES = 4
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "libraries.json")


def get(page):
    # Use Libby's own page size: asking for a bigger one silently skipped libraries.
    url = f"{API}?page={page}"
    for attempt in range(MAX_TRIES):
        try:
            with urlopen(Request(url, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
            wait = 2 ** attempt * 3
            print(f"page {page}: {e}; retrying in {wait}s", flush=True)
            time.sleep(wait)
    raise SystemExit(f"Gave up on page {page}")


def main():
    libs, seen, total_seen = [], set(), 0
    first = get(1) or {}
    expected = int(first.get("totalItems") or 0)
    last = int(((first.get("links") or {}).get("last") or {}).get("page") or MAX_PAGES)
    print(f"Libby lists {expected} libraries over {last} pages", flush=True)
    for page in range(1, min(last, MAX_PAGES) + 1):
        items = (first if page == 1 else (get(page) or {})).get("items") or []
        if not items:
            break
        for it in items:
            total_seen += 1
            key = str(it.get("id") or "").strip()
            name = str(it.get("name") or "").strip()
            if not key or not name or key in seen:
                continue
            if str(it.get("status") or "").lower() != "live":
                continue
            seen.add(key)
            libs.append([key, name])
        if page % 25 == 0:
            print(f"page {page}: {total_seen} read, {len(libs)} live", flush=True)
        time.sleep(DELAY)

    # A broken run must never replace a good file with a partial one.
    if expected and total_seen < expected * 0.98:
        raise SystemExit(f"Read only {total_seen} of {expected} listed libraries; keeping the previous file.")
    if len(libs) < int(os.environ.get("MIN_LIBRARIES", "1000")):
        raise SystemExit(f"Only {len(libs)} live libraries found; keeping the previous file.")

    libs.sort(key=lambda x: x[1].lower())
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out = {"built": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "n": len(libs), "libs": libs}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(libs)} live libraries (of {total_seen} listed) to data/libraries.json")


if __name__ == "__main__":
    sys.exit(main())
