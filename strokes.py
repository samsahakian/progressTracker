#!/usr/bin/env python3
"""Fetch per-stroke data for every Logbook result into data/strokes/<id>.json (cached between runs).

Standard library only. Needs C2_TOKEN. Only results without a cached file are fetched, so after the
first run this is a handful of requests a day.

Each cached file is a compact list of [seconds, meters, hr] per stroke (hr 0 when not recorded).
"""
import json
import os
import urllib.error
import sys
import time
from pathlib import Path

from sync import BASE, get

ROOT = Path(__file__).parent
RESULTS = ROOT / "data" / "results.json"
OUT = ROOT / "data" / "strokes"


def normalize(raw):
    """Concept2 stroke rows -> [[t_seconds, d_meters, hr], ...]. t is tenths of a second, d decimeters."""
    rows = raw.get("data", raw) if isinstance(raw, dict) else raw
    out = []
    for s in rows or []:
        t, d = s.get("t"), s.get("d")
        if t is None or d is None:
            continue
        out.append([round(t / 10, 1), round(d / 10, 1), int(s.get("hr") or 0)])
    return out


def main():
    token = os.environ.get("C2_TOKEN")
    if not token:
        sys.exit("Set C2_TOKEN.")
    OUT.mkdir(parents=True, exist_ok=True)
    results = json.loads(RESULTS.read_text())
    todo = [r for r in results if r.get("stroke_data", True) and not (OUT / f"{r['id']}.json").exists()]
    print(f"{len(results)} results, {len(todo)} without cached stroke data")
    sample_shown = False
    for i, r in enumerate(todo, 1):
        try:
            raw = get(f"{BASE}/api/users/me/results/{r['id']}/strokes", token)
        except urllib.error.HTTPError as e:  # e.g. 404: no stroke data for this result
            print(f"  result {r['id']}: no stroke data ({e.code})")
            raw = []
        if not sample_shown:  # field names only, to spot format changes
            rows = raw.get("data", raw) if isinstance(raw, dict) else raw
            print("Stroke fields:", sorted(rows[0]) if rows else "(empty)")
            sample_shown = True
        (OUT / f"{r['id']}.json").write_text(json.dumps(normalize(raw), separators=(",", ":")))
        if i % 50 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(0.3)
    print(f"Cached stroke data for {len(list(OUT.glob('*.json')))} results")


if __name__ == "__main__":
    main()
