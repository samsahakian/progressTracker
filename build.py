#!/usr/bin/env python3
"""Turn data/results.json into a static dashboard at site/index.html.

Standard library only. Benchmark rules live in config.json.
"""
import argparse
import re
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
        "start": (r.get("date") or "")[:16],
        "rest_s": sum((iv.get("rest_time") or 0) / 10 for iv in ((r.get("workout") or {}).get("intervals") or [])),
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
    return f"{max(5, round(w['seconds'] / 300) * 5)} min"  # merged with exact "N min" (coach G17)


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


TYPE_NAMES = {"threshold": "Threshold", "race_pace": "Race-pace", "test": "Test", "hard": "Hard", "steady": "Steady", "other": "Other"}
TAGS = [("#steady", "steady"), ("#threshold", "threshold"), ("#race", "race_pace"), ("#test", "test"), ("#other", "other")]


def classify(w, rules, interval_hr_first=True):
    """Coach's session types for an erg result -> (type, source, low_confidence).

    HR-first for continuous pieces (steady pace drifts with fitness; HR describes effort).
    Order: test -> steady -> threshold -> race_pace -> other. Logbook distance/time for
    interval workouts are work-only, so the split is the work split.
    """
    for tag, typ in TAGS:
        if tag in w["comments"]:
            return typ, "tag", False
    sp, mins = w["split"], w["seconds"] / 60
    work_hr = (w.get("sm") or {}).get("avg_hr") or w["hr"]  # stroke HR covers work only
    hr = work_hr if work_hr and work_hr >= rules.get("min_valid_hr", 100) else None
    test_max, race_max = secs(rules["test_max_split"]), secs(rules["race_max_split"])
    steady_min = secs(rules["steady_min_split"])
    near = lambda edge: abs(sp - edge) < rules.get("low_confidence_seconds", 1.0)
    standard = (w["meters"] in rules["test_distances"]
                or any(abs(w["seconds"] - m * 60) <= 5 for m in rules["test_minutes"]))

    if not is_single_piece(w):  # intervals: HR-first, then work split
        if (interval_hr_first and hr is not None and hr <= rules["steady_hr_max"]
                and mins >= rules.get("interval_steady_min_minutes", 20)):
            return "steady", "inferred", False
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


def clean_hr(track, jump=20, within=5):
    """Drop HR readings that jump more than `jump` bpm from the last good reading in under `within` s (coach #2.1)."""
    out, last_t, last_hr, dropped = [], None, None, 0
    for t, d, hr in track:
        if hr and last_hr and abs(hr - last_hr) > jump and t - last_t < within:
            out.append((t, d, 0))
            dropped += 1
            continue
        if hr:
            last_t, last_hr = t, hr
        out.append((t, d, hr))
    return out, dropped


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
    timed = sum(zsec.values())
    if timed >= 0.8 * (timed + nohr) and timed > 0:
        hr_sum = 0.0
        for a, b in zip(strokes, strokes[1:]):
            dt = b[0] - a[0]
            if 0 < dt < 30 and b[2] >= min_valid:
                hr_sum += b[2] * dt
        out["avg_hr"] = round(hr_sum / timed, 1)
    out["z3plus_s"] = round(sum(v for z, v in zsec.items() if z not in set(ath.get("easy_zones", ["Z1", "Z2"]))))
    end = strokes[-1][0] if strokes else 0
    w0, w1 = (m * 60 for m in ath.get("efficiency_window_min", [5, 35]))
    if end >= w1 - 5:
        out["win"] = window(strokes, w0, w1, min_valid)

    def drift_between(a0, a1, a2):
        h1, h2 = window(strokes, a0, a1, min_valid), window(strokes, a1, a2, min_valid)
        if not (h1 and h2):
            return None, None
        ef1, ef2 = h1["speed"] / h1["hr"], h2["speed"] / h2["hr"]
        return round((ef1 - ef2) / ef1 * 100, 2), [[round(h1["split"], 1), round(h1["hr"])], [round(h2["split"], 1), round(h2["hr"])]]

    if end >= 45 * 60 - 5:  # previous window (min 5-45), kept for comparison in the build log
        out["drift_5_45"], _ = drift_between(300, 1500, min(2700, end))
    d0, d1 = (m * 60 for m in ath.get("drift_window_min", [10, 50]))
    if end >= d1 - 5:  # fixed window, so short and long pieces compare fairly (coach E13, #2.2)
        out["drift"], out["halves"] = drift_between(d0, (d0 + d1) / 2, min(d1, end))
        if out["drift"] is None:
            del out["drift"], out["halves"]
        whole, _ = drift_between(d0, (d0 + end) / 2, end)  # whole-piece drift, for the tooltip
        if whole is not None:
            out["drift_whole"] = whole
    return out


def lthr_estimate(strokes, lt, min_valid):
    """LTHR from a 30-min test: time-weighted avg HR over minutes 10-30 (Friel field test).

    Returns None when stroke HR covers < 90% of the window. Also reports pace for the opening and
    the window, so a faded (not all-out) piece can be flagged.
    """
    w0, w1 = (m * 60 for m in lt.get("window_min", [10, 30]))
    end = strokes[-1][0] if strokes else 0
    if end < w1 - 10:
        return None
    win = window(strokes, w0, min(w1, end), min_valid)
    if not win:
        return None
    opening = window(strokes, 0, w0, 0)  # pace only; HR coverage doesn't matter here
    hrs = [p[2] for p in strokes if w0 <= p[0] <= w1 and p[2] >= min_valid]
    return {"hr": round(win["hr"], 1), "split": round(win["split"], 1),
            "open_split": round(opening["split"], 1) if opening else None,
            "max_hr": max(hrs) if hrs else None}


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


def tod_bucket(hhmm):
    """Coach I19: early (< 8:00), midday (8:00-16:00), evening (>= 16:00)."""
    if not hhmm or ":" not in hhmm:
        return None
    h = int(hhmm[:2])
    return "early" if h < 8 else "midday" if h < 16 else "evening"


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


def load_water(path):  # noqa: E302
    """On-water sessions from data/water.json (written by garmin.py), if present."""
    if not path.exists():
        return []
    out = []
    for a in json.loads(path.read_text()):
        if not a.get("meters") or not a.get("seconds"):
            continue
        split = a["seconds"] / a["meters"] * 500
        out.append({"id": f"g{a['id']}", "date": a["date"][:10], "start_t": a["date"][11:16], "name": a.get("name", ""),
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


def build(results, cfg, manual_catchup=(), water=(), strokes_dir=None, garmin_daily=None, garmin_acts=(), garmin_indoor=(),
          hevy=None):
    everything = [w for w in (parse(r) for r in results) if w] + list(manual_catchup)
    everything.sort(key=lambda w: w["date"])
    workouts = [w for w in everything if not w.get("volume_only")]  # timed pieces only
    rules = cfg.get("session_types")
    # Per-stroke metrics (time in zone, efficiency window, drift) where stroke data is cached.
    ath = cfg.get("athlete", {})
    min_valid = (rules or {}).get("min_valid_hr", 100)
    with_strokes = 0
    for w in workouts:
        st = load_strokes(strokes_dir / f"{w['id']}.json") if strokes_dir else None
        w["sm"] = stroke_metrics(st, ath, min_valid) if st and ath.get("zones") else None
        with_strokes += bool(w["sm"])

    type_counts, type_counts_before = defaultdict(int), defaultdict(int)
    for w in workouts:
        w["stype"], w["stype_src"], w["stype_low"] = classify(w, rules) if rules else ("other", "inferred", False)
        type_counts[w["stype"]] += 1
        if rules:
            type_counts_before[classify(w, rules, interval_hr_first=False)[0]] += 1

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
    # On-water HR from Garmin FIT tracks joins the same weekly buckets (time in zone only: on-water
    # speed depends on wind and current, so no drift or efficiency from it).
    fit_dir = strokes_dir.parent / "fit" if strokes_dir else None
    water_with_hr = 0
    for w in water:
        p = fit_dir / f"{w['id'][1:]}.json" if fit_dir else None
        if not p or not p.exists():
            continue
        tr = json.loads(p.read_text())
        if not tr:
            continue
        sm = stroke_metrics([tuple(x) for x in tr], {**ath, "drift_min_minutes": 10**6, "efficiency_window_min": [10**5, 10**5]}, min_valid)
        if not sm["zones"]:
            continue
        water_with_hr += 1
        d = date.fromisoformat(w["date"])
        x = wk[(d - timedelta(days=d.weekday())).isoformat()]
        x["water_sessions"] = x.get("water_sessions", 0) + 1
        x["nohr"] += sm["nohr"]
        for z, secs_ in sm["zones"].items():
            x["zones"][z] += secs_
            x["water_s"] = x.get("water_s", 0) + secs_
            x["easy" if z in easy_z else "moderate" if z in mod_z else "hard"] += secs_
    # Title overrides for Garmin sessions (coach A3)
    kw = (rules or {}).get("title_keywords", {})

    def title_type(name):
        n = (name or "").lower()
        return next((t for k, t in kw.items() if re.search(rf"\b{re.escape(k)}\b", n)), None)

    for w in water:
        w["type"] = title_type(w.get("name")) or "water"

    # Garmin-only indoor rowing: club practices never logged to Concept2 (coach B4-B7).
    # Time and HR only: their meters never count toward goals or totals.
    indoor_only, dedupe_counts = [], None
    if garmin_indoor:
        import dedupe
        matched, how = dedupe.match(garmin_indoor, results)
        dedupe_counts = {"garmin_indoor": len(garmin_indoor), "matched": sum(1 for v in matched.values() if v),
                         "garmin_only": sum(1 for v in matched.values() if not v), **{f"by_{k}": v for k, v in how.items()}}
        hard_min, hard_hr = ath.get("garmin_hard_min", 8), ath.get("garmin_hard_hr", 150)
        artifact_log = []
        for g in garmin_indoor:
            if matched[g["id"]]:
                continue
            if (g["elapsed"] or g["seconds"]) < ath.get("garmin_min_minutes", 10) * 60:  # fragment: hidden, not typed (#2.5)
                indoor_only.append({**g, "type": "fragment", "type_src": "", "low": False, "sm": None})
                continue
            p = fit_dir / f"{g['id']}.json" if fit_dir else None
            tr = [tuple(x) for x in json.loads(p.read_text())] if p and p.exists() else []
            raw_sm = stroke_metrics(tr, {**ath, "efficiency_window_min": [10**5, 10**5]}, min_valid) if tr else None
            tr, dropped = clean_hr(tr, ath.get("hr_artifact_bpm", 20), ath.get("hr_artifact_seconds", 5))
            sm = stroke_metrics(tr, {**ath, "efficiency_window_min": [10**5, 10**5]}, min_valid) if tr else None
            if sm and not sm["zones"]:
                sm = None
            tt, z3 = title_type(g["name"]), None
            if tt:
                typ, src, low = tt, "title", False
            else:
                src, low = "inferred", True
                avg = (sm or {}).get("avg_hr") or g.get("hr")
                z3 = sum(v for z, v in (sm or {}).get("zones", {}).items() if z not in easy_z) if sm else None
                if z3 is not None and z3 >= hard_min * 60:
                    typ = "hard"
                elif avg and avg < hard_hr:
                    typ = "steady"
                else:
                    typ = "other"
            indoor_only.append({**g, "type": typ, "type_src": src, "low": low, "sm": sm})
            if raw_sm:
                z3_raw = sum(v for z, v in raw_sm["zones"].items() if z not in easy_z)
                artifact_log.append((g["date"], g["name"], dropped, round(z3_raw / 60, 1), round((z3 or 0) / 60, 1),
                                     raw_sm.get("avg_hr"), (sm or {}).get("avg_hr"), typ))
            if sm:
                d = date.fromisoformat(g["date"])
                x = wk[(d - timedelta(days=d.weekday())).isoformat()]
                x["indoor_sessions"] = x.get("indoor_sessions", 0) + 1
                x["nohr"] += sm["nohr"]
                for z, secs_ in sm["zones"].items():
                    x["zones"][z] += secs_
                    x["indoor_s"] = x.get("indoor_s", 0) + secs_
                    x["easy" if z in easy_z else "moderate" if z in mod_z else "hard"] += secs_
    intensity = [{"week": k, **{kk: (dict(vv) if kk == "zones" else vv) for kk, vv in v.items()}} for k, v in sorted(wk.items())]

    # Local start time for every erg session: the Logbook time is usually the finish time (coach I19 needs starts).
    kind_of = dedupe.match.kind_of if garmin_indoor else {}
    for w in workouts:
        try:
            t = datetime.fromisoformat(w["start"])
        except ValueError:
            w["start_t"] = None
        else:
            if kind_of.get(w["id"]) != "start":
                t -= timedelta(seconds=w["seconds"] + w.get("rest_s", 0))
            w["start_t"] = t.strftime("%H:%M")
        w["tod"] = tod_bucket(w["start_t"])
    for w in water:
        w["tod"] = tod_bucket(w.get("start_t"))
    for g in indoor_only:
        g["start_t"] = (g.get("start") or "")[11:16]
        g["tod"] = tod_bucket(g["start_t"])

    # Hard sessions and training time per week, all sources (coach C8, D10)
    HARD = {"threshold", "race_pace", "test", "hard"}
    hard_w = defaultdict(lambda: {"erg": 0, "water": 0, "indoor": 0})
    time_w = defaultdict(lambda: {"erg": 0, "water": 0, "indoor": 0})
    monday = lambda ds: (date.fromisoformat(ds) - timedelta(days=date.fromisoformat(ds).weekday())).isoformat()
    for w in workouts:
        time_w[monday(w["date"])]["erg"] += w["seconds"] + w.get("rest_s", 0)
        hard_w[monday(w["date"])]["erg"] += w["stype"] in HARD
    for w in water:
        time_w[monday(w["date"])]["water"] += w["seconds"]
        hard_w[monday(w["date"])]["water"] += w["type"] in HARD
    for g in indoor_only:
        time_w[monday(g["date"])]["indoor"] += g["elapsed"] or g["seconds"]
        hard_w[monday(g["date"])]["indoor"] += g["type"] in HARD
    load_time = [{"week": k, **{kk: round(vv / 60) for kk, vv in v.items()}} for k, v in sorted(time_w.items())]

    # Hard rows with local start times, for the lifting tab's "heavy lower body before a hard row" check.
    # Garmin-only indoor sessions are club practices and count as hard whatever their type (coach Q6).
    hard_rows = []
    for w in workouts:
        if w["stype"] in HARD and w.get("start_t"):
            hard_rows.append({"dt": datetime.fromisoformat(f"{w['date']}T{w['start_t']}"), "label": f"{TYPE_NAMES[w['stype']]} erg", "id": w["id"]})
    for w in water:
        if w["type"] in HARD and w.get("start_t"):
            hard_rows.append({"dt": datetime.fromisoformat(f"{w['date']}T{w['start_t']}"), "label": "Hard on-water"})
    for g in indoor_only:
        if g["type"] != "fragment" and g.get("start_t"):
            hard_rows.append({"dt": datetime.fromisoformat(f"{g['date']}T{g['start_t']}"), "label": "Club indoor (Garmin)"})
    lifting_data = None
    if hevy and hevy.get("workouts"):
        import lifting
        lifting_data = lifting.lifting_payload(hevy["workouts"], hevy.get("templates"), hevy.get("mapping", {}), garmin_acts,
                                               hard_rows, cfg, cfg.get("timezone", "America/Chicago"))
    hard_weeks = [{"week": k, **v, "n": sum(v.values())} for k, v in sorted(hard_w.items())]

    # Aerobic efficiency (steady only, fixed window) and drift (steady, long enough)
    efficiency = [{"date": w["date"], "minutes": round(w["seconds"] / 60), "split": round(w["sm"]["win"]["split"], 1),
                   "hr": round(w["sm"]["win"]["hr"], 1), "rate": w["rate"], "tod": w["tod"], "start": w["start_t"],
                   "w_hr": round(watts_from_split(w["sm"]["win"]["split"]) / w["sm"]["win"]["hr"], 3)}
                  for w in workouts if w["stype"] == "steady" and w["sm"] and w["sm"].get("win")]
    drift = [{"date": w["date"], "minutes": round(w["seconds"] / 60), "drift": w["sm"]["drift"], "halves": w["sm"]["halves"],
              "whole": w["sm"].get("drift_whole"), "old": w["sm"].get("drift_5_45"), "split": w["split"], "hr": w["hr"], "src": "Erg",
              "tod": w["tod"], "start": w["start_t"]}
             for w in workouts if w["stype"] == "steady" and w["sm"] and "drift" in w["sm"]]
    for g in indoor_only:
        if g["type"] == "steady" and g["sm"] and "drift" in g["sm"]:
            drift.append({"date": g["date"], "minutes": round((g["elapsed"] or g["seconds"]) / 60), "drift": g["sm"]["drift"],
                          "halves": g["sm"]["halves"], "whole": g["sm"].get("drift_whole"), "split": None, "hr": g.get("hr"),
                          "src": "Garmin indoor", "tod": g["tod"], "start": g["start_t"]})
    drift.sort(key=lambda r: r["date"])
    def quarter(ds):
        return f"{ds[:4]}-Q{(int(ds[5:7]) - 1) // 3 + 1}"
    hb = ath.get("efficiency_hr_band", [142, 149])
    tod_report = []
    for q in sorted({quarter(r["date"]) for r in efficiency}):
        rows = [r for r in efficiency if quarter(r["date"]) == q]
        counts = {b: sum(r["tod"] == b for r in rows) for b in ("early", "midday", "evening")}
        def med(vals):
            vals = sorted(vals)
            return vals[len(vals) // 2] if vals else None
        pace = {b: med([r["split"] for r in rows if r["tod"] == b and hb[0] <= r["hr"] <= hb[1] + 0.99]) for b in ("early", "evening")}
        whr = {b: med([r["w_hr"] for r in rows if r["tod"] == b]) for b in ("early", "evening")}
        tod_report.append({"quarter": q, "counts": counts,
                           "pace_diff": round(pace["early"] - pace["evening"], 1) if None not in pace.values() else None,
                           "whr_diff": round(whr["early"] - whr["evening"], 3) if None not in whr.values() else None,
                           "pace_n": [sum(r["tod"] == b and hb[0] <= r["hr"] <= hb[1] + 0.99 for r in rows) for b in ("early", "evening")]})
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
    # Benchmark pieces with a same-session lift before them get an "after lift" marker (coach review 2); not excluded.
    after_lift = {h["row_id"]: h["gap_h"] for h in (lifting_data or {}).get("pre_hard", []) if h["kind"] == "same" and h.get("row_id")}
    format_groups = []
    for label, rows in formats.items():
        run_best, marked = None, []
        for w in rows:
            pb = run_best is None or w["split"] < run_best
            run_best = w["split"] if pb else run_best
            rest = [iv[2] for iv in w["intervals"]]
            marked.append({**w, "pb": pb, "rest": round(sum(rest) / len(rest)) if rest else None, "start": w.get("start_t"),
                           "after_lift": after_lift.get(w["id"])})
        types = defaultdict(int)
        for w in rows:
            types[w["stype"]] += 1
        format_groups.append({"id": "f" + hashlib.md5(label.encode()).hexdigest()[:8], "label": label,
                              "type": max(types, key=types.get), "rows": marked})
    format_groups.sort(key=lambda g: (-len(g["rows"]), g["label"]))

    # --- LTHR estimates from 30-min test pieces (avg HR, minutes 10-30) ---
    lt = ath.get("lthr_test", {})
    lt_min = lt.get("minutes", 30)
    capped_ids = {w["id"] for b in cfg["benchmarks"] if b.get("max_rate") for w in bench[b["id"]]["rows"]}
    lthr = []
    for w in workouts:
        if "#nolthr" in w["comments"] or not is_single_piece(w) or abs(w["seconds"] - lt_min * 60) > 5:
            continue
        if not ("#lthr" in w["comments"] or w["stype"] == "test" or w["id"] in benchmark_ids):
            continue
        st = load_strokes(strokes_dir / f"{w['id']}.json") if strokes_dir else None
        est = None
        if st:
            st, _ = clean_hr(st, ath.get("hr_artifact_bpm", 20), ath.get("hr_artifact_seconds", 5))
            est = lthr_estimate(st, lt, min_valid)
        row = {"date": w["date"], "start": w["start_t"], "tod": w["tod"], "split": w["split"], "watts": w["watts"],
               "rate": w["rate"], "avg_hr": w["hr"], "label": bench_of.get(w["id"]) or workout_format(w),
               "capped": w["id"] in capped_ids or bool(w["rate"] and w["rate"] <= lt.get("capped_max_rate", 21)),
               "est": est}
        if est and est["open_split"]:
            row["faded"] = est["split"] - est["open_split"] > lt.get("fade_seconds", 3)
        lthr.append(row)
        row["tagged"] = "#lthr" in w["comments"]
    valid = [r for r in lthr if r["est"]]
    clean = [r for r in valid if not r["capped"] and not r.get("faded")]
    # Which pieces count toward the headline (coach LTHR feedback #1): all-out uncapped pieces; a #lthr tag; and a
    # rate-capped piece whose estimate is >= the latest all-out estimate before it (a low rate can only pull HR down).
    last_clean = None
    for r in valid:
        if not r["capped"] and not r.get("faded"):
            r["counts"], r["why"] = True, "all-out"
            last_clean = r["est"]["hr"]
        elif r["tagged"]:
            r["counts"], r["why"] = True, "tagged #lthr"
        elif r["capped"] and not r.get("faded") and last_clean is not None and r["est"]["hr"] >= last_clean:
            r["counts"], r["why"] = True, f"rate-capped, at or above the last all-out ({last_clean:g})"
        else:
            r["counts"], r["why"] = False, ("faded" if r.get("faded") else "" if not r["capped"]
                                            else "rate-capped, no all-out piece before it" if last_clean is None else "rate-capped, below the last all-out")
    counted = [r for r in valid if r["counts"]]
    lthr_summary = {"pieces": len(lthr), "valid": len(valid), "clean": len(clean), "counted": len(counted),
                    "latest": counted[-1] if counted else None, "latest_any": valid[-1] if valid else None}

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
        "lthr": lthr,
        "lthr_summary": lthr_summary,
        "strokes_coverage": [with_strokes, len(workouts)],
        "water_hr_coverage": [water_with_hr, len(water)],
        "hard_weeks": hard_weeks,
        "tod_report": tod_report,
        "time_weeks": load_time,
        "dedupe": dedupe_counts,
        "artifacts": artifact_log if garmin_indoor else [],
        "indoor_counts": {t: sum(g["type"] == t for g in indoor_only) for t in ("hard", "steady", "other", "threshold", "race_pace", "test")},
        "events": cfg.get("events", []),
        "lifting": lifting_data,
        "athlete": cfg.get("athlete", {}),
        "summary": {
            "total_meters": total_m,
            "workouts": len(workouts),
            "catchup_entries": len(catchup),
            "catchup_meters": sum(w["meters"] for w in catchup),
            "types": dict(type_counts),
            "types_before": dict(type_counts_before),
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
              "type": w["stype"], "type_src": w["stype_src"], "low": w["stype_low"],
              "short": w["seconds"] + w.get("rest_s", 0) < ath.get("short_piece_min", 5) * 60,
              "tod": w["tod"], "start": w["start_t"]} for w in workouts] +
            [{"kind": "Water", "date": w["date"], "name": w["name"], "meters": w["meters"],
              "seconds": w["seconds"], "split": w["split"], "rate": w["rate"], "hr": w["hr"],
              "type": w["type"], "type_src": "title" if w["type"] != "water" else "", "low": False,
              "short": w["seconds"] < ath.get("short_piece_min", 5) * 60, "tod": w["tod"], "start": w.get("start_t")} for w in water] +
            [{"kind": "Indoor (Garmin)", "date": g["date"], "name": g["name"], "meters": None,
              "seconds": g["elapsed"] or g["seconds"], "split": None, "rate": g.get("rate"),
              "hr": (g["sm"] or {}).get("avg_hr") or g.get("hr"), "type": g["type"], "type_src": g["type_src"],
              "low": g["low"], "short": g["type"] == "fragment" or (g["elapsed"] or g["seconds"]) < ath.get("short_piece_min", 5) * 60,
              "tod": g["tod"], "start": g["start_t"]}
             for g in indoor_only],
            key=lambda r: r["date"]),
        "seasons": season_lines[-4:],  # last four seasons keeps the chart readable
        "benchmarks": [{**g, "rows": [strip(w) for w in g["rows"]]} for g in format_groups],
        "steady": [strip(w) for w in steady],
        "steady_cfg": {"windows": windows, "default": str(ss.get("default_window", 60)),
                       "rolling": ss.get("rolling_sessions", 5), "min_minutes": ss["min_minutes"]},
    }


def load_hevy(data_dir):
    """Hevy workouts and templates (written by hevy.py) plus the lifts.csv mapping, if present."""
    hw, ht = data_dir / "hevy_workouts.json", data_dir / "hevy_templates.json"
    if not hw.exists():
        return None
    import lifting
    return {"workouts": json.loads(hw.read_text()), "templates": json.loads(ht.read_text()) if ht.exists() else [],
            "mapping": lifting.load_mapping(ROOT / "lifts.csv")}


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
                    json.loads(ga.read_text()) if ga.exists() else [],
                    json.loads(gi.read_text()) if (gi := args.data.parent / "garmin_indoor.json").exists() else [],
                    load_hevy(args.data.parent))
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
    print(f"On-water HR tracks: {payload['water_hr_coverage'][0]}/{payload['water_hr_coverage'][1]} sessions")
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
    ls = payload["lthr_summary"]
    print(f"LTHR test pieces: {ls['pieces']} ({ls['valid']} with stroke HR, {ls['clean']} all-out & uncapped, {ls['counted']} count toward the headline):",
          [(r["date"], r["label"], (r["est"] or {}).get("hr"), "capped" if r["capped"] else "", "faded" if r.get("faded") else "",
            "COUNTS" if r.get("counts") else "", r.get("why", "")) for r in payload["lthr"]])
    if ls["latest"]:
        print(f"LTHR headline candidate: {ls['latest']['est']['hr']} on {ls['latest']['date']} ({ls['latest']['label']}, {ls['latest']['why']})")
    print("Benchmark formats:", ", ".join(f"{g['label']} ({len(g['rows'])})" for g in payload["benchmarks"]))
    print("Time of day (steady sessions in the efficiency window, early/midday/evening; early-minus-evening median pace at HR s, W/HR):")
    for r in payload["tod_report"]:
        print(f"  {r['quarter']}: {r['counts']} pace diff {r['pace_diff']} (n {r['pace_n']}), W/HR diff {r['whr_diff']}")
    for a_ in payload["artifacts"]:
        if a_[2] or a_[0] == "2025-07-04":
            print(f"  HR artifacts {a_[0]} '{a_[1]}': dropped {a_[2]} readings; Z3+ {a_[3]} -> {a_[4]} min; avg HR {a_[5]} -> {a_[6]}; type {a_[7]}")
    print("Drift, recent: date, min 10-50 (new), min 5-45 (old), whole piece:",
          [(r["date"], r["drift"], r.get("old"), r.get("whole")) for r in payload["drift"][-6:]])
    print(f"Garmin indoor dedupe: {payload['dedupe']}; Garmin-only types: {payload['indoor_counts']}")
    L = payload["lifting"]
    if L:
        lw = L["weeks"][-4:]
        print(f"Lifting: {len(L['sessions'])} sessions ({sum(s['prehab'] for s in L['sessions'])} prehab, {L['garmin']['unlogged']} unlogged Garmin); "
              f"Garmin strength matched {L['garmin']['matched']}; series {', '.join(f'{k} ({len(v)})' for k, v in L['e1rm'].items())}")
        print("  last 4 weeks lifts/prehab/sets:", [(w["week"], w["lifts"], w["prehab"], w["sets"]) for w in lw])
        print("  unmapped (accessory, guessed pattern):", [(u["name"], u["pattern"], u["how"], u["n"]) for u in L["unmapped"]])
        print("  heavy lower body before a hard row:", [(h["lift_date"], h["row_date"], h["row"], h["gap_h"], h["kind"]) for h in L["pre_hard"]])
        print("  benchmark pieces after a same-session lift:", [(r["date"], g["label"], r["after_lift"]) for g in payload["benchmarks"] for r in g["rows"] if r.get("after_lift") is not None])
    print(f"Session types before interval HR-first: {s['types_before']}")
    print(f"Session types: {s['types']} · tagged {s['types_tagged']}, inferred {s['workouts'] - s['types_tagged']}, "
          f"low-confidence {s['types_low']}")


if __name__ == "__main__":
    main()
