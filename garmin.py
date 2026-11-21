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
ACT_OUT = Path(__file__).parent / "data" / "garmin_activities.json"
INDOOR_OUT = Path(__file__).parent / "data" / "garmin_indoor.json"
RESULTS = Path(__file__).parent / "data" / "results.json"
DAILY_OUT = Path(__file__).parent / "data" / "garmin_daily.json"
DAILY_DAYS = 400  # history for resting HR, sleep and HRV
FIT_DIR = Path(__file__).parent / "data" / "fit"  # cached [[t, m, hr], ...] per on-water session
PAGE = 100


def call(method, path, headers, params=None, raw=False):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "c2-progress/1.0", **headers})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return body if raw else body.decode()
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

    rows, types, start, shown_fields, acts, indoor = [], {}, 0, False, [], []
    while True:
        batch = json.loads(call("GET", SEARCH_PATH, headers, {"start": start, "limit": PAGE}) or "[]")
        if isinstance(batch, dict):  # tolerate a wrapped list
            batch = batch.get("activityList") or batch.get("activities") or []
        for a in batch:
            types[type_key(a)] = types.get(type_key(a), 0) + 1
            acts.append({"date": (a.get("startTimeLocal") or "")[:10], "type": type_key(a),
                         "load": a.get("activityTrainingLoad"), "minutes": round((a.get("duration") or 0) / 60, 1)})
            if "indoor_row" in type_key(a).lower():
                indoor.append({
                    "id": a.get("activityId"), "start": (a.get("startTimeLocal") or "")[:19],
                    "date": (a.get("startTimeLocal") or "")[:10], "name": a.get("activityName") or "",
                    "elapsed": round(pick(a, "elapsedDuration", "duration") or 0),
                    "seconds": round(pick(a, "movingDuration", "duration") or 0),
                    "meters": round(a.get("distance") or 0),
                    "hr": pick(a, "averageHR", "averageHeartRate"), "max_hr": pick(a, "maxHR", "maxHeartRate"),
                    "rate": stroke_rate(a), "load": a.get("activityTrainingLoad"),
                })
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
    acts.sort(key=lambda a: a["date"])
    indoor.sort(key=lambda a: a["start"])
    INDOOR_OUT.write_text(json.dumps(indoor))
    garmin_only = indoor
    if RESULTS.exists():
        import dedupe
        matched, how = dedupe.match(indoor, json.loads(RESULTS.read_text()))
        garmin_only = [g for g in indoor if not matched[g["id"]]]
        print(f"Garmin indoor rowing: {len(indoor)} sessions, {len(indoor) - len(garmin_only)} match a Logbook session "
              f"(by start {how['start']}, by finish {how['end']}), {len(garmin_only)} Garmin-only")
    ACT_OUT.write_text(json.dumps(acts))
    print(f"Saved {len(acts)} activities (date, type, training load) to {ACT_OUT}")
    try:
        fit_tracks(garmin_only + rows, headers)  # Garmin-only indoor first (coach priority), then on-water
    except (Exception, SystemExit) as e:  # optional; never lose the rowing or daily data over it
        print(f"::warning::Garmin FIT download skipped: {e}")
    try:
        daily(headers)
    except SystemExit as e:  # wellness data is optional; never lose the rowing data over it
        print(f"::warning::Garmin daily data skipped: {e}")


def fit_tracks(rows, headers):
    """Download and cache HR/distance tracks for on-water sessions (only new ones)."""
    import fitlite
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [r for r in rows if r["id"] and not (FIT_DIR / f"{r['id']}.json").exists()]
    print(f"FIT tracks: {len(rows) - len(todo)} cached, {len(todo)} to download")
    for r in todo:
        data = call("GET", f"/download-service/files/activity/{r['id']}",
                    {**headers, "Accept": "application/zip, application/x-zip-compressed, application/octet-stream"}, raw=True)
        try:
            tr = fitlite.track(data)
        except Exception as e:
            print(f"  {r['id']}: could not read FIT ({e})")
            tr = []
        (FIT_DIR / f"{r['id']}.json").write_text(json.dumps(tr, separators=(",", ":")))
        time.sleep(0.3)
    print(f"FIT tracks cached: {len(list(FIT_DIR.glob('*.json')))}")


def paths(obj, prefix=""):
    """Key paths of a JSON object (no values), to log the shape of an unfamiliar response."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from paths(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list) and obj:
        yield from paths(obj[0], prefix + "[]")
    else:
        yield prefix


def find(obj, *names):
    """First numeric value under any key whose name matches one of names (case-insensitive, depth-first)."""
    want = [n.lower() for n in names]
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in want and isinstance(v, (int, float)) and v:
                return v
        for v in obj.values():
            r = find(v, *names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find(v, *names)
            if r is not None:
                return r
    return None


def day_rows(resp):
    rows = resp.get("data", resp) if isinstance(resp, dict) else resp
    if isinstance(rows, dict):  # some endpoints wrap the list one level deeper
        rows = next((v for v in rows.values() if isinstance(v, list)), [rows])
    return rows or []


def day_of(row):
    for k in ("calendarDate", "date", "summaryDate"):
        v = row.get(k) if isinstance(row, dict) else None
        if isinstance(v, str):
            return v[:10]
        if isinstance(v, dict) and isinstance(v.get("date"), str):
            return v["date"][:10]
    return None


def daily(headers):
    """Resting HR, sleep and HRV per day, in 28-day chunks."""
    from datetime import date, timedelta
    end, out, shown = date.today(), {}, set()
    sources = [
        ("rhr", "/usersummary-service/v2/dailysummaries", "query",
         ("restingHeartRate", "restingHR", "restingValue", "currentDayRestingHeartRate")),
        ("sleep_s", "/sleep-service/sleep/dailySleepSummariesByDate", "query",
         ("sleepTimeSeconds", "totalSleepTimeInSeconds", "sleepDurationInSeconds", "durationInSeconds", "totalSleepSeconds")),
        ("hrv", "/hrv-service/hrv/daily", "path", ("lastNightAvg", "lastNight", "nightlyAvg", "weeklyAvg")),
    ]
    for field, path, style, names in sources:
        got, d = 0, end - timedelta(days=DAILY_DAYS)
        while d <= end:
            e = min(d + timedelta(days=27), end)
            if style == "query":
                text = call("GET", path, headers, {"startDate": d.isoformat(), "endDate": e.isoformat()})
            else:
                text = call("GET", f"{path}/{d.isoformat()}/{e.isoformat()}", headers)
            resp = json.loads(text or "null") or {}
            for row in day_rows(resp):
                if field not in shown and isinstance(row, dict):
                    print(f"{field} response fields:", ", ".join(sorted(set(paths(row))))[:1500])
                    shown.add(field)
                day, val = day_of(row), find(row, *names)
                if day and val is not None:
                    out.setdefault(day, {})[field] = val
                    got += 1
            d = e + timedelta(days=1)
            time.sleep(0.3)
        print(f"Daily {field}: {got} days")
    DAILY_OUT.write_text(json.dumps(dict(sorted(out.items()))))
    print(f"Saved daily resting HR / sleep / HRV for {len(out)} days to {DAILY_OUT}")


if __name__ == "__main__":
    main()
