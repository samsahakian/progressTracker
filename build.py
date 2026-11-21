#!/usr/bin/env python3
"""Turn data/results.json into a static dashboard at site/index.html.

Standard library only. Benchmark rules live in config.json.
"""
import argparse
import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
        "splits": parse_splits(r),
    }


def parse_splits(r):
    """[(seconds, meters, hr or None), ...] from the result's recorded splits."""
    out = []
    for sp in ((r.get("workout") or {}).get("splits") or []):
        secs = (sp.get("time") or 0) / 10.0
        hr_info = sp.get("heart_rate") or {}
        hr = hr_info.get("average") or hr_info.get("ending") or None
        if secs > 0:
            out.append((secs, sp.get("distance") or 0, hr if hr and hr >= 50 else None))
    return out


def first_minutes(w, minutes, min_hr_coverage=0.9):
    """Pace, HR and W/HR over the first `minutes` of a piece, from its splits.

    None when the piece is shorter than that, or HR covers < 90% of the window.
    """
    target = minutes * 60
    if w["seconds"] < target - 5 or not w["splits"]:
        return None
    t = m = hr_t = hr_sum = 0.0
    for secs, dist, hr in w["splits"]:
        take = min(secs, target - t)
        if take <= 0:
            break
        frac = take / secs
        t += take
        m += dist * frac
        if hr:
            hr_t += take
            hr_sum += hr * take
    if t < target - 5 or m <= 0 or hr_t < min_hr_coverage * t:
        return None
    split = t / m * 500
    watts = watts_from_split(split)
    hr = hr_sum / hr_t
    return {"split": round(split, 1), "watts": round(watts, 1), "hr": round(hr, 1),
            "w_hr": round(watts / hr, 3)}


def secs(mmss):
    m, sec = str(mmss).split(":")
    return int(m) * 60 + float(sec)


TAGS = [("#steady", "steady"), ("#threshold", "threshold"), ("#race", "race_pace"), ("#test", "test"), ("#other", "other")]


def classify(w, rules):
    """Coach's session types for an erg result -> (type, source, low_confidence).

    HR-first for continuous pieces (steady pace drifts with fitness; HR describes effort).
    Order: test -> steady -> threshold -> race_pace -> other. Logbook distance/time for
    interval workouts are work-only, so the split is the work split.
    """
    for tag, typ in TAGS:
        if tag in w["comments"]:
            return typ, "tag", False
    sp, mins = w["split"], w["seconds"] / 60
    hr = w["hr"] if w["hr"] and w["hr"] >= rules.get("min_valid_hr", 100) else None
    test_max, race_max = secs(rules["test_max_split"]), secs(rules["race_max_split"])
    steady_min = secs(rules["steady_min_split"])
    near = lambda edge: abs(sp - edge) < rules.get("low_confidence_seconds", 1.0)
    standard = (w["meters"] in rules["test_distances"]
                or any(abs(w["seconds"] - m * 60) <= 5 for m in rules["test_minutes"]))

    if not is_single_piece(w):  # intervals: by work split
        if sp <= race_max:
            return "race_pace", "inferred", near(race_max)
        if sp < steady_min:
            return "threshold", "inferred", near(race_max) or near(steady_min)
        return "other", "inferred", near(steady_min)

    if standard and sp <= test_max:  # tests first, so a low strap reading can't make a test "steady"
        return "test", "inferred", near(test_max)
    if mins >= rules["steady_min_minutes"]:
        if hr is not None:
            if hr <= rules["steady_hr_max"]:
                return "steady", "inferred", False
            if sp >= steady_min and hr <= rules["steady_max_hr"]:  # slow piece, HR in Z3: original steady rule
                return "steady", "inferred", near(steady_min)
        elif sp >= steady_min:  # no usable HR: pace rule, low confidence
            return "steady", "inferred", True
    if mins >= rules["threshold_min_minutes"] and race_max < sp < steady_min:
        if hr is None:
            return "threshold", "inferred", True
        if hr >= rules["threshold_min_hr"]:
            return "threshold", "inferred", near(steady_min) or near(race_max)
    return "other", "inferred", hr is None


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


def load_catchup(path):
    """Catchup meters listed by hand in catchup.csv (the API doesn't return them)."""
    if not path.exists():
        return []
    lines = [l for l in path.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    out = []
    for i, row in enumerate(csv.DictReader(lines)):
        out.append({"id": f"catchup-{i}", "date": date.fromisoformat(row["date"].strip()).isoformat(),
                    "meters": int(row["meters"].replace(",", "")), "seconds": 0, "volume_only": True})
    return out


def load_water(path):
    """On-water sessions from data/water.json (written by garmin.py), if present."""
    if not path.exists():
        return []
    out = []
    for a in json.loads(path.read_text()):
        if not a.get("meters") or not a.get("seconds"):
            continue
        split = a["seconds"] / a["meters"] * 500
        out.append({"id": f"g{a['id']}", "date": a["date"][:10], "name": a.get("name", ""),
                    "meters": a["meters"], "seconds": a["seconds"], "split": round(split, 1),
                    "hr": round(a["hr"]) if a.get("hr") else None,
                    "max_hr": round(a["max_hr"]) if a.get("max_hr") else None,
                    "rate": round(a["rate"], 1) if a.get("rate") else None,
                    "dps": round(a["dps"], 2) if a.get("dps") else None})
    return out


def build(results, cfg, manual_catchup=(), water=()):
    everything = [w for w in (parse(r) for r in results) if w] + list(manual_catchup)
    everything.sort(key=lambda w: w["date"])
    workouts = [w for w in everything if not w.get("volume_only")]  # timed pieces only
    rules = cfg.get("session_types")
    for w in workouts:
        w["stype"], w["stype_src"], w["stype_low"] = classify(w, rules) if rules else ("other", "inferred", False)
    type_counts = defaultdict(int)
    for w in workouts:
        type_counts[w["stype"]] += 1
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
    windows = ss.get("windows", [40, 60, 65, 70, 75, 80])
    steady = []
    for w in workouts:
        if (w["id"] in benchmark_ids or not is_single_piece(w)
                or w["seconds"] < ss["min_minutes"] * 60
                or not w["rate"] or w["rate"] > ss["max_rate"]):
            continue
        win = {str(n): v for n in windows if (v := first_minutes(w, n))}
        if w["w_hr"] or win:
            steady.append({**w, "win": win})

    # --- Volume ---
    start_month = cfg.get("season_start_month", 5)
    weekly = defaultdict(int)
    weekly_water = defaultdict(int)
    for w in water:
        d = date.fromisoformat(w["date"])
        weekly_water[(d - timedelta(days=d.weekday())).isoformat()] += w["meters"]
    seasons = defaultdict(lambda: defaultdict(int))
    seasons_water = defaultdict(lambda: defaultdict(int))
    for w in water:  # catchup stays out: it's mostly practice that Garmin already has
        d = date.fromisoformat(w["date"])
        ws = season_of(d, start_month)
        seasons_water[ws][(d - date(ws - 1, start_month, 1)).days] += w["meters"]
    # Catchup meters count toward lifetime and weekly meters but, as on the Logbook,
    # not toward season meters.
    for w in everything:
        d = date.fromisoformat(w["date"])
        monday = d - timedelta(days=d.weekday())
        weekly[monday.isoformat()] += w["meters"]
        if w.get("volume_only"):
            continue
        s = season_of(d, start_month)
        season_start = date(s - 1, start_month, 1)
        seasons[s][(d - season_start).days] += w["meters"]

    # fill empty weeks so gaps show as zero rather than being skipped
    weeks = []
    keys = set(weekly) | set(weekly_water)
    if keys:
        cur = date.fromisoformat(min(keys))
        last = date.fromisoformat(max(keys))
        while cur <= last:
            k = cur.isoformat()
            weeks.append({"week": k, "meters": weekly.get(k, 0), "water": weekly_water.get(k, 0)})
            cur += timedelta(days=7)

    def cumulative(days):
        total, pts = 0, []
        for day in sorted(days):
            total += days[day]
            pts.append([day, total])
        return pts, total

    season_lines = []
    for s in sorted(set(seasons) | set(seasons_water)):
        both = defaultdict(int)
        for src in (seasons.get(s, {}), seasons_water.get(s, {})):
            for day, m in src.items():
                both[day] += m
        line = {"season": f"{s - 1}–{str(s)[-2:]}"}
        for key, days in (("", seasons.get(s, {})), ("water_", seasons_water.get(s, {})), ("all_", both)):
            line[key + "points"], line[key + "total"] = cumulative(days)
        season_lines.append(line)

    total_m = sum(w["meters"] for w in everything)  # lifetime, incl. catchup
    strip = lambda w: {k: v for k, v in w.items() if k not in ("comments", "splits", "stype_low", "stype_src")}
    # Daily meters for goal pacing. Erg = Logbook timed results only (no catchup), like
    # Concept2's challenge count; water = Garmin outdoor rowing.
    daily = defaultdict(lambda: {"erg": 0, "water": 0})
    for w in workouts:
        daily[w["date"]]["erg"] += w["meters"]
    for w in water:
        daily[w["date"]]["water"] += w["meters"]

    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    return {
        "generated": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        "daily": dict(sorted(daily.items())),
        "goals": cfg.get("goals", []),
        "events": cfg.get("events", []),
        "athlete": cfg.get("athlete", {}),
        "summary": {
            "total_meters": total_m,
            "workouts": len(workouts),
            "catchup_entries": len(catchup),
            "catchup_meters": sum(w["meters"] for w in catchup),
            "types": dict(type_counts),
            "types_tagged": sum(w["stype_src"] == "tag" for w in workouts),
            "types_low": sum(w["stype_low"] for w in workouts),
            "water_sessions": len(water),
            "water_meters": sum(w["meters"] for w in water),
            "water_year": sum(w["meters"] for w in water if w["date"][:4] == str(date.today().year)),
            "first": workouts[0]["date"] if workouts else None,
            "last": workouts[-1]["date"] if workouts else None,
        },
        "weeks": weeks,
        "water": list(water),
        "sessions": sorted(
            [{"kind": "Erg", "date": w["date"], "name": w["workout_type"], "meters": w["meters"],
              "seconds": w["seconds"], "split": w["split"], "rate": w["rate"], "hr": w["hr"],
              "type": w["stype"], "type_src": w["stype_src"], "low": w["stype_low"]} for w in workouts] +
            [{"kind": "Water", "date": w["date"], "name": w["name"], "meters": w["meters"],
              "seconds": w["seconds"], "split": w["split"], "rate": w["rate"], "hr": w["hr"],
              "type": "water", "type_src": "", "low": False} for w in water],
            key=lambda r: r["date"]),
        "seasons": season_lines[-4:],  # last four seasons keeps the chart readable
        "benchmarks": [
            {"id": k, "label": v["label"], "rows": [strip(w) for w in v["rows"]]}
            for k, v in bench.items() if v["rows"]
        ],
        "steady": [strip(w) for w in steady],
        "steady_cfg": {"windows": windows, "default": str(ss.get("default_window", 60)),
                       "rolling": ss.get("rolling_sessions", 5), "min_minutes": ss["min_minutes"]},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "results.json")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "index.html")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text())
    water = load_water(args.data.parent / "water.json")
    payload = build(json.loads(args.data.read_text()), cfg, load_catchup(ROOT / "catchup.csv"), water)
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
    counts = {n: sum(str(n) in w["win"] for w in payload["steady"]) for n in payload["steady_cfg"]["windows"]}
    print("Steady-state pieces per first-N-minutes window:", counts)
    print(f"On-water sessions: {s['water_sessions']} ({s['water_meters']:,} m)")
    print(f"Session types: {s['types']} · tagged {s['types_tagged']}, inferred {s['workouts'] - s['types_tagged']}, "
          f"low-confidence {s['types_low']}")


if __name__ == "__main__":
    main()
