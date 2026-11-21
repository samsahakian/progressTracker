#!/usr/bin/env python3
"""Write fake Logbook data to data/sample.json so the dashboard can be tried without a token.

python tools/make_sample.py && python build.py --data data/sample.json
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

random.seed(7)
out = []
rid = 1000


def add(d, meters, secs, rate, hr, wtype="JustRow", comments=""):
    global rid
    rid += 1
    out.append({
        "id": rid, "date": f"{d} 07:00:00", "type": "rower", "workout_type": wtype,
        "distance": meters, "time": int(secs * 10), "stroke_rate": rate,
        "heart_rate": {"average": hr} if hr else None, "comments": comments,
    })


def secs_for(meters, split):
    return meters / 500 * split


start, end = date(2024, 11, 1), date(2026, 11, 8)
d, i = start, 0
while d <= end:
    fit = (d - start).days / (end - start).days  # 0 -> 1 over two years
    dow = d.weekday()
    if dow in (0, 2, 4, 6) and random.random() < 0.8:  # steady state
        mins = random.choice([30, 40, 45, 60, 80])
        split = 128 - 4 * fit + random.uniform(-1.5, 1.5)
        hr = round(142 - 3 * fit + random.uniform(-4, 4))
        meters = round(mins * 60 / split * 500)
        add(d, meters, mins * 60, random.choice([18, 19, 20]), hr if random.random() > 0.15 else None)
    if dow == 3 and random.random() < 0.7:  # intervals
        split = 116 - 4 * fit + random.uniform(-1, 1)
        add(d, 6000, secs_for(6000, split), 24, 160, "FixedDistanceInterval")
    if dow == 5 and d.day <= 7:  # monthly 30r20
        split = 119.5 - 4 * fit + random.uniform(-1.2, 1.2)
        add(d, round(1800 / split * 500), 1800, 20, round(158 + random.uniform(-3, 3)))
    if dow == 5 and 15 <= d.day <= 21 and d.month % 3 == 0:  # quarterly 2k
        add(d, 2000, secs_for(2000, 105 - 3 * fit + random.uniform(-1, 1)), 31, 176)
    if dow == 5 and 15 <= d.day <= 21 and d.month % 3 == 1:
        add(d, 1000, secs_for(1000, 100 - 3 * fit + random.uniform(-1, 1)), 34, 172)
    if dow == 5 and 22 <= d.day <= 28 and d.month in (1, 6):
        add(d, 6000, secs_for(6000, 112 - 3 * fit + random.uniform(-1, 1)), 27, 170)
        add(d - timedelta(days=60), 5000, secs_for(5000, 111 - 3 * fit), 28, 171)
    d += timedelta(days=1)

path = Path(__file__).resolve().parent.parent / "data" / "sample.json"
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(out))
print(f"Wrote {len(out)} fake results to {path}")
