#!/usr/bin/env python3
"""Pull Hevy workouts and exercise templates into data/hevy_workouts.json and data/hevy_templates.json.

Standard library only. Needs HEVY_API_KEY (Hevy Pro: hevy.com/settings?developer, or the app's
Settings -> Developer). Weights come from the API in kilograms whatever unit the app shows.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("HEVY_API_BASE_URL", "https://api.hevyapp.com/v1")
DATA = Path(__file__).parent / "data"
WORKOUTS_OUT = DATA / "hevy_workouts.json"
TEMPLATES_OUT = DATA / "hevy_templates.json"


def get(path, key, params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"api-key": key, "Accept": "application/json", "User-Agent": "c2-progress/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404 and params.get("page", 1) > 1:
                return {}  # past the last page
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(3 * 2 ** attempt)
                continue
            detail = e.read().decode(errors="replace")[:300]
            if e.code in (401, 403):
                sys.exit(f"Hevy rejected the API key ({e.code}). It needs Hevy Pro; check the HEVY_API_KEY secret. {detail}")
            sys.exit(f"Hevy request failed ({e.code}) for {path}: {detail}")


def paged(path, key, list_key, page_size):
    out, page = [], 1
    while True:
        body = get(path, key, {"page": page, "pageSize": page_size})
        items = body.get(list_key) or []
        out.extend(items)
        count = body.get("page_count") or page
        print(f"  {path}: page {page}/{count}, {len(out)} so far", file=sys.stderr)
        if not items or page >= count:
            return out
        page += 1
        time.sleep(0.3)


def main():
    key = os.environ.get("HEVY_API_KEY")
    if not key:
        sys.exit("Set HEVY_API_KEY.")
    workouts = paged("/workouts", key, "workouts", 10)  # the API caps workouts at 10 per page
    workouts.sort(key=lambda w: w.get("start_time") or "")
    templates = paged("/exercise_templates", key, "exercise_templates", 100)
    DATA.mkdir(parents=True, exist_ok=True)
    WORKOUTS_OUT.write_text(json.dumps(workouts))
    TEMPLATES_OUT.write_text(json.dumps([{k: t.get(k) for k in ("id", "title", "type", "primary_muscle_group",
                                                                   "secondary_muscle_groups", "equipment", "is_custom")}
                                         for t in templates]))
    names = sorted({e.get("title") for w in workouts for e in w.get("exercises") or [] if e.get("title")})
    print(f"Saved {len(workouts)} Hevy workouts ({workouts[0]['start_time'][:10] if workouts else '—'} – "
          f"{workouts[-1]['start_time'][:10] if workouts else '—'}) and {len(templates)} exercise templates")
    print("Exercises seen:", "; ".join(names))


if __name__ == "__main__":
    main()
