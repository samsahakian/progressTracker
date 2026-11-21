#!/usr/bin/env python3
"""Pull every result from your Concept2 Logbook into data/results.json.

Standard library only, so it runs anywhere Python 3.9+ does (including GitHub Actions)
without installing anything.

Token: set C2_TOKEN (personal access token from log.concept2.com -> Settings -> Developer).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://log.concept2.com"
OUT = Path(__file__).parent / "data" / "results.json"


def get(url, token):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.c2logbook.v1+json",
            "User-Agent": "c2-progress/1.0",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("401 from Concept2: the token is missing, wrong, or expired.")
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise


def fetch_all(token, machine, since=None):
    params = {"per_page": 250}
    if machine != "all":
        params["type"] = machine
    if since:
        params["from"] = since
    url = f"{BASE}/api/users/me/results?{urllib.parse.urlencode(params)}"
    results = []
    while url:
        page = get(url, token)
        results.extend(page.get("data", []))
        links = (page.get("meta") or {}).get("pagination", {}).get("links") or {}
        url = links.get("next") if isinstance(links, dict) else None  # [] on a single page
        print(f"  fetched {len(results)} results…", file=sys.stderr)
        if url:
            time.sleep(0.5)  # be polite to Concept2
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--machine", default="rower", help="rower, skierg, bike or all (default rower)")
    ap.add_argument("--since", help="only fetch results on/after YYYY-MM-DD")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    token = os.environ.get("C2_TOKEN")
    if not token:
        sys.exit("Set C2_TOKEN to your Concept2 personal access token.")

    results = fetch_all(token, args.machine, args.since)

    # Merge with anything already on disk so --since runs are incremental.
    existing = {}
    if args.out.exists():
        existing = {r["id"]: r for r in json.loads(args.out.read_text())}
    for r in results:
        existing[r["id"]] = r
    merged = sorted(existing.values(), key=lambda r: r.get("date", ""))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, indent=1))
    print(f"Saved {len(merged)} results to {args.out}")


if __name__ == "__main__":
    main()
