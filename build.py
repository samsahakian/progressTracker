#!/usr/bin/env python3
"""Turn data/results.json into a static dashboard at site/index.html.

Standard library only. Benchmark rules live in config.json.
"""
import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
INTERVAL_TYPES = {"FixedDistanceInterval", "FixedTimeInterval", "FixedCalorieInterval",
                  "VariableInterval", "VariableIntervalUndefinedRest", "FixedWattMinuteInterval"}


def watts_from_split(split_500):
    """Concept2's pace->power formula. split_500 in seconds per 500 m."""
    pace = split_500 / 500.0
    return 2.80 / pace ** 3


def parse(r):
    """Normalise one Logbook result into the fields we use."""
    dist = r.get("distance") or 0
    secs = (r.get("time") or 0) / 10.0  # Logbook time is in tenths of a second
    if dist <= 0:
        return None
    if secs <= 0:
        # Meters with no time, e.g. Logbook catchup entries: count toward volume only.
        return {"id": r["id"], "date": (r.get("date") or "")[:10], "meters": dist,
                "seconds": 0, "volume_only": True}
    split = secs / dist * 500
    hr = (r.get("heart_rate") or {}).get("average") or None
    if hr is not None and hr < 50:  # dropouts / bogus values
        hr = None
    watts = watts_from_split(split)
    return {
        "id": r["id"],
        "date": (r.get("date") or "")[:10],
        "type": r.get("type", "rower"),
        "workout_type": r.get("workout_type", ""),
        "meters": dist,
        "seconds": round(secs, 1),
        "split": round(split, 1),
        "watts": round(watts, 1),
        "rate": r.get("stroke_rate"),
        "hr": hr,
        "w_hr": round(watts / hr, 3) if hr else None,
        "comments": (r.get("comments") or "").lower(),
    }


def is_single_piece(w):
    return w["workout_type"] not in INTERVAL_TYPES


def easy_baseline(workouts):
    """For each workout, median watts of easy continuous pieces in the prior 60 days."""
    easy = [(date.fromisoformat(w["date"]), w["watts"]) for w in workouts
            if is_single_piece(w) and w["seconds"] >= 20 * 60 and w["rate"] and w["rate"] <= 22]
    out = {}
    for w in workouts:
        d = date.fromisoformat(w["date"])
        window = sorted(p for day, p in easy if d - timedelta(days=60) <= day < d)
        out[w["id"]] = window[len(window) // 2] if len(window) >= 3 else None
    return out


def matches(w, b, baseline=None):
    tags = w["comments"]
    if "#notest" in tags:
        return False
    if f"#{b['id'].lower()}" in tags:
        return True
    if "min_effort" in b and baseline and w["watts"] < baseline * b["min_effort"]:
        return False
    if not is_single_piece(w):
        return False
    if "meters" in b and w["meters"] != b["meters"]:
        return False
    if "minutes" in b and abs(w["seconds"] - b["minutes"] * 60) > 5:
        return False
    rate = w["rate"] or 0
    if "max_rate" in b and (not rate or rate > b["max_rate"]):
        return False
    if "min_rate" in b and rate < b["min_rate"]:
        return False
    return True


def season_of(d, start_month):
    """Concept2 seasons run May 1 - Apr 30 and are named for the ending year."""
    return d.year + 1 if d.month >= start_month else d.year


def build(results, cfg):
    everything = sorted((w for w in (parse(r) for r in results) if w), key=lambda w: w["date"])
    workouts = [w for w in everything if not w.get("volume_only")]  # timed pieces only
    catchup = [w for w in everything if w.get("volume_only")]

    # --- Benchmarks (first matching rule wins, so 30r20 beats the 30-min test) ---
    bench = {b["id"]: {"label": b["label"], "rows": []} for b in cfg["benchmarks"]}
    benchmark_ids = set()
    base = easy_baseline(workouts)
    for w in workouts:
        for b in cfg["benchmarks"]:
            if matches(w, b, base[w["id"]]):
                bench[b["id"]]["rows"].append(w)
                benchmark_ids.add(w["id"])
                break
    for b in bench.values():
        # mark running PBs (each row that beat everything before it)
        run_best = None
        marked = []
        for w in b["rows"]:
            pb = run_best is None or w["split"] < run_best
            if pb:
                run_best = w["split"]
            marked.append({**w, "pb": pb})
        b["rows"] = marked

    # --- Steady-state efficiency (W/HR) ---
    ss = cfg["steady_state"]
    steady = [
        w for w in workouts
        if w["id"] not in benchmark_ids
        and is_single_piece(w)
        and w["seconds"] >= ss["min_minutes"] * 60
        and w["rate"] and w["rate"] <= ss["max_rate"]
        and w["w_hr"]
    ]
    n = ss.get("rolling_sessions", 5)
    for i, w in enumerate(steady):
        window = steady[max(0, i - n + 1): i + 1]
        w["w_hr_avg"] = round(sum(x["w_hr"] for x in window) / len(window), 3)

    # --- Volume ---
    start_month = cfg.get("season_start_month", 5)
    weekly = defaultdict(int)
    seasons = defaultdict(lambda: defaultdict(int))
    # Catchup meters count toward lifetime meters but, as on the Logbook, not toward
    # season meters, so weekly and season charts use timed workouts only.
    for w in workouts:
        d = date.fromisoformat(w["date"])
        monday = d - timedelta(days=d.weekday())
        weekly[monday.isoformat()] += w["meters"]
        s = season_of(d, start_month)
        season_start = date(s - 1, start_month, 1)
        seasons[s][(d - season_start).days] += w["meters"]

    # fill empty weeks so gaps show as zero rather than being skipped
    weeks = []
    if weekly:
        cur = date.fromisoformat(min(weekly))
        last = date.fromisoformat(max(weekly))
        while cur <= last:
            weeks.append({"week": cur.isoformat(), "meters": weekly.get(cur.isoformat(), 0)})
            cur += timedelta(days=7)

    season_lines = []
    for s in sorted(seasons):
        total, pts = 0, []
        for day in sorted(seasons[s]):
            total += seasons[s][day]
            pts.append([day, total])
        season_lines.append({"season": f"{s - 1}–{str(s)[-2:]}", "points": pts, "total": total})

    total_m = sum(w["meters"] for w in everything)  # lifetime, incl. catchup
    strip = lambda w: {k: v for k, v in w.items() if k != "comments"}
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "summary": {
            "total_meters": total_m,
            "workouts": len(workouts),
            "catchup_entries": len(catchup),
            "catchup_meters": sum(w["meters"] for w in catchup),
            "first": workouts[0]["date"] if workouts else None,
            "last": workouts[-1]["date"] if workouts else None,
        },
        "weeks": weeks,
        "seasons": season_lines[-4:],  # last four seasons keeps the chart readable
        "benchmarks": [
            {"id": k, "label": v["label"], "rows": [strip(w) for w in v["rows"]]}
            for k, v in bench.items() if v["rows"]
        ],
        "steady": [strip(w) for w in steady],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "results.json")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "index.html")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text())
    payload = build(json.loads(args.data.read_text()), cfg)
    html = (ROOT / "template.html").read_text().replace(
        "/*__DATA__*/null", json.dumps(payload, separators=(",", ":"))
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    s = payload["summary"]
    print(f"Built {args.out}: {s['workouts']} workouts, {s['total_meters']:,} m, "
          f"{sum(len(b['rows']) for b in payload['benchmarks'])} benchmark pieces, "
          f"{len(payload['steady'])} steady-state sessions, "
          f"{s['catchup_entries']} catchup/untimed entries ({s['catchup_meters']:,} m)")


if __name__ == "__main__":
    main()
