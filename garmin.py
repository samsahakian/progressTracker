#!/usr/bin/env python3
"""Pull on-water rowing sessions from the Garmin Connect Personal API into data/water.json.

Standard library only. Needs GARMIN_API_KEY (Garmin Connect -> Settings -> Personal API Key).
Endpoints follow Garmin's official client, github.com/garmin/connect-personal-api-python.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SEARCH_PATH = "/activitylist-service/activities/search/activities"
OUT = Path(__file__).parent / "data" / "water.json"
PAGE = 100


def call(method, path, headers, params=None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "c2-progress/1.0", **headers})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (401, 403) and path == TOKEN_PATH:
                sys.exit(f"Garmin rejected the API key ({e.code}). It may have expired; make a new one "
                         f"and update the GARMIN_API_KEY secret. {detail}")
            sys.exit(f"Garmin request failed ({e.code}) for {path}: {detail}")


def get_token(key):
    text = call("POST", TOKEN_PATH, {"Accept": "*/*", "Authorization": f"ApiKey {key}"}).strip()
    if text.startswith("{"):  # some accounts get JSON, others the bare token
        body = json.loads(text)
        text = body.get("access_token") or body.get("accessToken") or ""
    if not text:
        sys.exit("Garmin token response was empty.")
    print(f"::add-mask::{text}")
    return text


def pick(a, *keys):
    for k in keys:
        v = a.get(k)
        if v not in (None, 0, 0.0, ""):
            return v
    return None


def stroke_rate(a):
    """Average strokes/min. Garmin sends no average rate for rowing, so use strokes / moving time."""
    direct = pick(a, "avgStrokeCadence", "averageStrokeCadence", "avgStrokeRate")
    if direct:
        return direct
    strokes, secs = pick(a, "strokes"), pick(a, "movingDuration", "duration")
    return round(strokes / (secs / 60), 1) if strokes and secs else None


def type_key(a):
    t = a.get("activityType") or {}
    return (t.get("typeKey") if isinstance(t, dict) else str(t)) or ""


def is_on_water(a):
    k = type_key(a).lower()
    return "row" in k and "indoor" not in k and (a.get("distance") or 0) > 0


def main():
    key = os.environ.get("GARMIN_API_KEY")
    if not key:
        sys.exit("Set GARMIN_API_KEY.")
    token = get_token(key)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    rows, types, start, shown_fields = [], {}, 0, False
    while True:
        batch = json.loads(call("GET", SEARCH_PATH, headers, {"start": start, "limit": PAGE}) or "[]")
        if isinstance(batch, dict):  # tolerate a wrapped list
            batch = batch.get("activityList") or batch.get("activities") or []
        for a in batch:
            types[type_key(a)] = types.get(type_key(a), 0) + 1
            if not is_on_water(a):
                continue
            if not shown_fields:  # field names only, no values: helps adapt if Garmin renames things
                print("Rowing activity fields:", ", ".join(sorted(a)), file=sys.stderr)
                shown_fields = True
            rows.append({
                "id": a.get("activityId"),
                "date": (a.get("startTimeLocal") or "")[:19],
                "name": a.get("activityName") or "",
                "type": type_key(a),
                "meters": round(a.get("distance") or 0),
                "seconds": round(pick(a, "movingDuration", "duration") or 0),
                "elapsed": round(pick(a, "elapsedDuration", "duration") or 0),
                "hr": pick(a, "averageHR", "averageHeartRate"),
                "max_hr": pick(a, "maxHR", "maxHeartRate"),
                "rate": stroke_rate(a),
                "strokes": pick(a, "strokes", "totalNumberOfStrokes"),
                "dps": pick(a, "avgStrokeDistance"),  # meters per stroke
                "device": a.get("deviceId"),
            })
        print(f"  scanned {start + len(batch)} activities…", file=sys.stderr)
        if len(batch) < PAGE:
            break
        start += PAGE
        time.sleep(0.5)

    print("Activity types seen:", ", ".join(f"{k or '?'} ({n})" for k, n in sorted(types.items())))
    rows.sort(key=lambda r: r["date"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=1))
    print(f"Saved {len(rows)} on-water sessions to {OUT}")


if __name__ == "__main__":
    main()
