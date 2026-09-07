#!/usr/bin/env python3
"""One-off diagnostic: which BGG endpoints answer from this machine?

The website is behind Cloudflare and blocks datacenter IPs. The XML API may or
may not be behind the same rules. This tells us which, without guessing.
"""

import ssl
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PLAIN_UA = "shelfworthy-cache/1.0"

TARGETS = [
    ("XML API, one game",   "https://boardgamegeek.com/xmlapi2/thing?id=224517&stats=1"),
    ("XML API, alt host",   "https://api.geekdo.com/xmlapi2/thing?id=224517&stats=1"),
    ("XML API, batch",      "https://boardgamegeek.com/xmlapi2/thing?id=224517,224037,174430&stats=1"),
    ("XML API, search",     "https://boardgamegeek.com/xmlapi2/search?query=brass&type=boardgame"),
    ("Website browse page", "https://boardgamegeek.com/browse/boardgame"),
]


def probe(label, url, ua):
    try:
        req = Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
        with urlopen(req, timeout=45, context=ssl.create_default_context()) as r:
            body = r.read()
            head = body[:80].decode("utf-8", "replace").replace("\n", " ")
            print(f"  {label:22s} {r.status}  {len(body):>7} bytes  {head[:60]}")
            return r.status == 200 and len(body) > 200
    except HTTPError as e:
        print(f"  {label:22s} {e.code}  BLOCKED ({e.reason})")
    except URLError as e:
        print(f"  {label:22s} ---  {e.reason}")
    return False


def main():
    results = {}
    for ua_name, ua in (("plain user-agent", PLAIN_UA), ("browser user-agent", BROWSER_UA)):
        print(f"\nWith a {ua_name}:")
        for label, url in TARGETS:
            results[(ua_name, label)] = probe(label, url, ua)

    api_ok = any(v for (ua, label), v in results.items() if label.startswith("XML API"))
    print("\n" + "=" * 60)
    if api_ok:
        working = sorted({label for (ua, label), v in results.items()
                          if v and label.startswith("XML API")})
        print("XML API IS REACHABLE. Working endpoints:")
        for w in working:
            print(f"  - {w}")
        print("\nSo the plan is: drop the website scraping, use only the API.")
    else:
        print("XML API IS ALSO BLOCKED from this machine.")
        print("GitHub Actions will not work for this. The job needs to run")
        print("somewhere with a residential IP instead.")
    print("=" * 60)
    return 0  # never fail the run; this is a diagnostic


if __name__ == "__main__":
    sys.exit(main())
