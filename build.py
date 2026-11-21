#!/usr/bin/env python3
"""Turn data/results.json into a static dashboard at site/index.html.

Standard library only. Benchmark rules live in config.json.
"""
import argparse
import hashlib
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
        "intervals": parse_intervals(r),
    }


def parse_intervals(r):
    """[(work_seconds, work_meters, rest_seconds, kind), ...] for interval workouts."""
    out = []
    for iv in ((r.get("workout") or {}).get("intervals") or []):
        out.append(((iv.get("time") or 0) / 10.0, iv.get("distance") or 0,
                    (iv.get("rest_time") or 0) / 10.0, iv.get("type") or ""))
    return out


def fmt_dist(m):
    return f"{m / 1000:g}k" if m >= 1000 and m % 100 == 0 else f"{m:g}m"


def fmt_dur(sec):
    sec = int(round(sec))
    return f"{sec // 60}min" if sec % 60 == 0 else f"{sec // 60}:{sec % 60:02d}"


def workout_format(w):
    """A label that identifies the workout format, e.g. '5×1k', '3×12min', '6k', 'Continuous ~25 min'."""
    ivs, wt = w["intervals"], w["workout_type"]
    if ivs:
        n = len(ivs)
        by_dist = wt == "FixedDistanceInterval" or (wt != "FixedTimeInterval" and all(k == "distance" for *_, k in ivs))
        vals = [round(d / 50) * 50 for _, d, _, _ in ivs] if by_dist else [round(t / 5) * 5 for t, _, _, _ in ivs]
        f = fmt_dist if by_dist else fmt_dur
        if len(set(vals)) == 1:
            return f"{n}×{f(vals[0])}"
        return "/".join(f(v) for v in vals) if n <= 6 else f"Variable, {n} intervals"
    if w["meters"] in (500, 1000, 2000, 5000, 6000, 10000, 21097):
        return fmt_dist(w["meters"])
    if wt == "FixedTimeSplits" or abs(w["seconds"] - round(w["seconds"] / 60) * 60) <= 2:
        return f"{round(w['seconds'] / 60)} min"
    return f"Continuous ~{max(5, round(w['seconds'] / 300) * 5)} min"


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


def load_strokes(path):
    """Cached [[t, d, hr], ...] for one result, with interval resets made continuous."""
    if not path.exists():
        return None
    rows = json.loads(path.read_text())
    out, t_off, d_off, pt, pd = [], 0.0, 0.0, 0.0, 0.0
    for t, d, hr in rows:
        if t < pt:  # next interval: Concept2 restarts time and distance
            t_off += pt
            d_off += pd
        pt, pd = t, d
        out.append((t + t_off, d + d_off, hr))
    return out


def zone_of(hr, zones):
    for name, top in zones:
        if hr <= top:
            return name
    return zones[-1][0]


def window(strokes, t0, t1, min_valid):
    """Speed (m/s), split and time-weighted HR between t0 and t1 seconds; None if HR covers < 90%."""
    pts = [p for p in strokes if t0 <= p[0] <= t1]
    if len(pts) < 10:
        return None
    dt_total = pts[-1][0] - pts[0][0]
    dist = pts[-1][1] - pts[0][1]
    if dt_total <= 0 or dist <= 0:
        return None
    hr_t = hr_sum = 0.0
    for a, b in zip(pts, pts[1:]):
        dt = b[0] - a[0]
        if 0 < dt < 30 and b[2] >= min_valid:
            hr_t += dt
            hr_sum += b[2] * dt
    if hr_t < 0.9 * dt_total:
        return None
    speed = dist / dt_total
    return {"speed": speed, "split": 500 / speed, "hr": hr_sum / hr_t}


def stroke_metrics(strokes, ath, min_valid):
    """Time in zone, the efficiency window, and drift for one session's strokes."""
    zones = ath["zones"]
    zsec, nohr = defaultdict(float), 0.0
    for a, b in zip(strokes, strokes[1:]):
        dt = b[0] - a[0]
        if not 0 < dt < 30:
            continue
        if b[2] >= min_valid:
            zsec[zone_of(b[2], zones)] += dt
        else:
            nohr += dt
    out = {"zones": {k: round(v) for k, v in zsec.items()}, "nohr": round(nohr)}
    end = strokes[-1][0] if strokes else 0
    w0, w1 = (m * 60 for m in ath.get("efficiency_window_min", [5, 35]))
    if end >= w1 - 5:
        out["win"] = window(strokes, w0, w1, min_valid)
    if end >= ath.get("drift_min_minutes", 30) * 60:
        mid = (w0 + end) / 2  # skip the first minutes, then compare halves
        h1, h2 = window(strokes, w0, mid, min_valid), window(strokes, mid, end, min_valid)
        if h1 and h2:
            ef1, ef2 = h1["speed"] / h1["hr"], h2["speed"] / h2["hr"]
            out["drift"] = round((ef1 - ef2) / ef1 * 100, 2)
            out["halves"] = [[round(h1["split"], 1), round(h1["hr"])], [round(h2["split"], 1), round(h2["hr"])]]
    return out


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


def recovery_series(daily, acts, ath):
    """Daily resting HR / sleep / HRV with the coach's baseline, plus weekly Garmin load and lifts."""
    b0, b1 = ath.get("rhr_baseline_days", [8, 37])
    flag_bpm = ath.get("rhr_flag_bpm", 4)
    days = sorted(daily)
    out = []
    for d in days:
        v = daily[d]
        dd = date.fromisoformat(d)
        past = [daily.get((dd - timedelta(days=k)).isoformat(), {}).get("rhr") for k in range(b0, b1 + 1)]
        past = sorted(x for x in past if x)
        last7 = [daily.get((dd - timedelta(days=k)).isoformat(), {}) for k in range(7)]
        r7 = [x["rhr"] for x in last7 if x.get("rhr")]
        s7 = [x["sleep_s"] for x in last7 if x.get("sleep_s")]
        out.append({
            "date": d, "rhr": v.get("rhr"), "hrv": v.get("hrv"),
            "sleep_h": round(v["sleep_s"] / 3600, 2) if v.get("sleep_s") else None,
            "rhr7": round(sum(r7) / len(r7), 1) if r7 else None,
            "sleep7": round(sum(s7) / len(s7) / 3600, 2) if s7 else None,
            "base": past[len(past) // 2] if len(past) >= 10 else None,
        })
    for r in out:
        r["high"] = bool(r["rhr"] and r["base"] and r["rhr"] >= r["base"] + flag_bpm)
    load, lifts = defaultdict(float), defaultdict(int)
    for a in acts:
        if not a.get("date"):
            continue
        d = date.fromisoformat(a["date"])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        load[wk] += a.get("load") or 0
        if "strength" in (a.get("type") or ""):
            lifts[wk] += 1
    weeks = sorted(set(load) | set(lifts))
    return out, [{"week": w, "load": round(load[w]), "lifts": lifts[w]} for w in weeks]


def build(results, cfg, manual_catchup=(), water=(), strokes_dir=None, garmin_daily=None, garmin_acts=()):
    everything = [w for w in (parse(r) for r in results) if w] + list(manual_catchup)
    everything.sort(key=lambda w: w["date"])
    workouts = [w for w in everything if not w.get("volume_only")]  # timed pieces only
    rules = cfg.get("session_types")
    for w in workouts:
        w["stype"], w["stype_src"], w["stype_low"] = classify(w, rules) if rules else ("other", "inferred", False)
    type_counts = defaultdict(int)
    for w in workouts:
        type_counts[w["stype"]] += 1

    # Per-stroke metrics (time in zone, efficiency window, drift) where stroke data is cached.
    ath = cfg.get("athlete", {})
    min_valid = (rules or {}).get("min_valid_hr", 100)
    with_strokes = 0
    for w in workouts:
        st = load_strokes(strokes_dir / f"{w['id']}.json") if strokes_dir else None
        w["sm"] = stroke_metrics(st, ath, min_valid) if st and ath.get("zones") else None
        with_strokes += bool(w["sm"])

    # Weekly intensity distribution (erg, from strokes)
    easy_z, mod_z = set(ath.get("easy_zones", ["Z1", "Z2"])), set(ath.get("moderate_zones", ["Z3"]))
    wk = defaultdict(lambda: {"easy": 0, "moderate": 0, "hard": 0, "nohr": 0, "sessions": 0, "zones": defaultdict(int)})
    for w in workouts:
        if not w["sm"]:
            continue
        d = date.fromisoformat(w["date"])
        x = wk[(d - timedelta(days=d.weekday())).isoformat()]
        x["sessions"] += 1
        x["nohr"] += w["sm"]["nohr"]
        for z, secs_ in w["sm"]["zones"].items():
            x["zones"][z] += secs_
            x["easy" if z in easy_z else "moderate" if z in mod_z else "hard"] += secs_
    intensity = [{"week": k, **{kk: (dict(vv) if kk == "zones" else vv) for kk, vv in v.items()}} for k, v in sorted(wk.items())]

    # Aerobic efficiency (steady only, fixed window) and drift (steady, long enough)
    efficiency = [{"date": w["date"], "minutes": round(w["seconds"] / 60), "split": round(w["sm"]["win"]["split"], 1),
                   "hr": round(w["sm"]["win"]["hr"], 1), "rate": w["rate"]}
                  for w in workouts if w["stype"] == "steady" and w["sm"] and w["sm"].get("win")]
    drift = [{"date": w["date"], "minutes": round(w["seconds"] / 60), "drift": w["sm"]["drift"], "halves": w["sm"]["halves"],
              "split": w["split"], "hr": w["hr"]}
             for w in workouts if w["stype"] == "steady" and w["sm"] and "drift" in w["sm"]]
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

    # --- Benchmark pieces grouped by format (coach P2): threshold, race pace and tests only ---
    bench_of = {w["id"]: v["label"] for v in bench.values() for w in v["rows"]}
    formats = defaultdict(list)
    for w in workouts:
        if w["stype"] in ("threshold", "race_pace", "test") or w["id"] in bench_of:
            formats[bench_of.get(w["id"]) or workout_format(w)].append(w)
    format_groups = []
    for label, rows in formats.items():
        run_best, marked = None, []
        for w in rows:
            pb = run_best is None or w["split"] < run_best
            run_best = w["split"] if pb else run_best
            rest = [iv[2] for iv in w["intervals"]]
            marked.append({**w, "pb": pb, "rest": round(sum(rest) / len(rest)) if rest else None})
        types = defaultdict(int)
        for w in rows:
            types[w["stype"]] += 1
        format_groups.append({"id": "f" + hashlib.md5(label.encode()).hexdigest()[:8], "label": label,
                              "type": max(types, key=types.get), "rows": marked})
    format_groups.sort(key=lambda g: (-len(g["rows"]), g["label"]))

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
    strip = lambda w: {k: v for k, v in w.items() if k not in ("comments", "splits", "intervals", "stype_low", "stype_src", "sm")}
    # Daily meters for goal pacing. Erg = Logbook timed results only (no catchup), like
    # Concept2's challenge count; water = Garmin outdoor rowing.
    daily = defaultdict(lambda: {"erg": 0, "water": 0})
    for w in workouts:
        daily[w["date"]]["erg"] += w["meters"]
    for w in water:
        daily[w["date"]]["water"] += w["meters"]

    recovery, load_weeks = recovery_series(garmin_daily or {}, garmin_acts, cfg.get("athlete", {}))
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    return {
        "generated": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        "daily": dict(sorted(daily.items())),
        "goals": cfg.get("goals", []),
        "intensity": intensity,
        "recovery": recovery,
        "load_weeks": load_weeks,
        "efficiency": efficiency,
        "drift": drift,
        "strokes_coverage": [with_strokes, len(workouts)],
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
        "benchmarks": [{**g, "rows": [strip(w) for w in g["rows"]]} for g in format_groups],
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
    gd, ga = args.data.parent / "garmin_daily.json", args.data.parent / "garmin_activities.json"
    payload = build(json.loads(args.data.read_text()), cfg, load_catchup(ROOT / "catchup.csv"), water,
                    args.data.parent / "strokes",
                    json.loads(gd.read_text()) if gd.exists() else {},
                    json.loads(ga.read_text()) if ga.exists() else [])
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
    cov = payload["strokes_coverage"]
    print(f"Stroke data: {cov[0]}/{cov[1]} sessions; {len(payload['efficiency'])} steady sessions in the efficiency window, "
          f"{len(payload['drift'])} with drift; {len(payload['intensity'])} weeks of zone data")
    if payload["intensity"]:
        last = payload["intensity"][-4:]
        print("Last weeks easy/mod/hard/nohr (min):", [(x["week"], x["easy"] // 60, x["moderate"] // 60, x["hard"] // 60, x["nohr"] // 60) for x in last])
    if payload["drift"]:
        print("Recent drift %:", [(x["date"], x["drift"]) for x in payload["drift"][-6:]])
    rec = payload["recovery"]
    if rec:
        tail = rec[-1]
        streak = 0
        for r in reversed(rec):
            if not r["high"]:
                break
            streak += 1
        print(f"Recovery: {len(rec)} days; latest {tail['date']} rhr {tail['rhr']} (7d {tail['rhr7']}, baseline {tail['base']}), "
              f"sleep7 {tail['sleep7']} h, hrv {tail['hrv']}; high-RHR streak {streak}; load weeks {len(payload['load_weeks'])}")
    print("Benchmark formats:", ", ".join(f"{g['label']} ({len(g['rows'])})" for g in payload["benchmarks"]))
    print(f"Session types: {s['types']} · tagged {s['types_tagged']}, inferred {s['workouts'] - s['types_tagged']}, "
          f"low-confidence {s['types_low']}")


if __name__ == "__main__":
    main()
