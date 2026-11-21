#!/usr/bin/env python3
"""Strava: OAuth helper and on-water rowing sync. Standard library only.

  python3 strava.py exchange CODE   one-time: trade the approval code for tokens
  python3 strava.py refresh         every run: trade the saved refresh token for fresh tokens
  python3 strava.py sync            fetch outdoor rowing activities into data/strava.json

Env: STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN (refresh), STRAVA_TOKEN (sync),
OUT_DIR (where tokens are written: strava_access_token, strava_refresh_token). Never prints a token.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
OUT = Path(__file__).parent / "data" / "strava.json"


def request(url, data=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": "c2-progress/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(60)  # Strava rate limit: wait and retry
                continue
            detail = e.read().decode(errors="replace")[:300]
            sys.exit(f"Strava refused the request ({e.code}): {detail}")


def auth(mode, code=None):
    fields = {"client_id": os.environ["STRAVA_CLIENT_ID"],
              "client_secret": os.environ["STRAVA_CLIENT_SECRET"]}
    if mode == "exchange":
        if "code=" in code:  # accept the whole redirect URL too
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
        fields.update(grant_type="authorization_code", code=code.strip())
    else:
        rt = os.environ.get("STRAVA_REFRESH_TOKEN")
        if not rt:
            sys.exit("STRAVA_REFRESH_TOKEN is empty. Run the 'Connect Strava' workflow first.")
        fields.update(grant_type="refresh_token", refresh_token=rt)
    tok = request(TOKEN_URL, fields)
    if "access_token" not in tok or "refresh_token" not in tok:
        sys.exit(f"Unexpected Strava token response; keys were {sorted(tok)}")
    out = Path(os.environ.get("OUT_DIR", "."))
    for name in ("access_token", "refresh_token"):
        print(f"::add-mask::{tok[name]}")
        (out / f"strava_{name}").write_text(tok[name])
    print("Got new Strava tokens.")


def is_on_water(a):
    """Outdoor rowing: rowing sport, not a trainer/virtual session, and has a GPS start."""
    sport = a.get("sport_type") or a.get("type")
    return (sport == "Rowing" and not a.get("trainer") and not a.get("manual")
            and a.get("start_latlng") and (a.get("distance") or 0) > 0)


def sync():
    token = os.environ.get("STRAVA_TOKEN")
    if not token:
        sys.exit("Set STRAVA_TOKEN.")
    rows, page = [], 1
    while True:
        batch = request(f"{API}/athlete/activities?per_page=200&page={page}", token=token)
        if not batch:
            break
        for a in batch:
            if not is_on_water(a):
                continue
            rows.append({
                "id": a["id"],
                "date": a.get("start_date_local", "")[:19].replace("T", " "),
                "name": a.get("name", ""),
                "meters": round(a.get("distance") or 0),
                "seconds": a.get("moving_time") or 0,
                "elapsed": a.get("elapsed_time") or 0,
                "hr": a.get("average_heartrate"),
                "max_hr": a.get("max_heartrate"),
                "rate": a.get("average_cadence"),
                "device": a.get("device_name"),
            })
        print(f"  scanned {page * 200 if len(batch) == 200 else (page - 1) * 200 + len(batch)} activities…",
              file=sys.stderr)
        page += 1
        time.sleep(0.5)
    rows.sort(key=lambda r: r["date"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=1))
    print(f"Saved {len(rows)} on-water sessions to {OUT}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "exchange" and len(sys.argv) > 2:
        auth("exchange", sys.argv[2])
    elif cmd == "refresh":
        auth("refresh")
    elif cmd == "sync":
        sync()
    else:
        sys.exit(__doc__)
