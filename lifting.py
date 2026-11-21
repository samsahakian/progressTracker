"""Lifting tab data from Hevy (coach spec 2026-11-21, claude/lifting-tab-coach-answers.md).

build.py calls lifting_payload(); standard library only.
"""
import csv
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KG_TO_LB = 2.2046226218
PATTERNS = ["squat", "hinge", "push", "pull", "core"]
HARD_SET_TYPES = {"normal", "failure"}
MUSCLE_PATTERN = {
    "quadriceps": "squat",
    "hamstrings": "hinge", "glutes": "hinge", "lower_back": "hinge",
    "chest": "push", "shoulders": "push", "triceps": "push",
    "lats": "pull", "upper_back": "pull", "biceps": "pull", "traps": "pull", "forearms": "pull",
    "abdominals": "core", "obliques": "core",
}
KEYWORD_PATTERN = [  # used when Hevy gives no muscle group
    (r"plank|crunch|pallof|dead ?bug|leg raise|ab wheel|sit ?up|bird ?dog|hollow|carry", "core"),
    (r"deadlift|\brdl\b|good morning|hip thrust|bridge|swing|hinge|back extension", "hinge"),
    (r"squat|lunge|split|step ?up|leg press", "squat"),
    (r"row|pull|chin|curl|lat ", "pull"),
    (r"press|push|dip|raise|fly", "push"),
]


def norm(name):
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def load_mapping(path):
    if not path.exists():
        return {}
    lines = [l for l in path.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return {norm(r["hevy_name"]): (r["series"].strip(), r["pattern"].strip().lower()) for r in csv.DictReader(lines)}


def classify(ex, mapping, templates):
    """(series, pattern, how) for one Hevy exercise; how is 'table', 'muscle' or 'name'."""
    name = ex.get("title") or ""
    if norm(name) in mapping:
        s, p = mapping[norm(name)]
        return s, p if p in PATTERNS else "other", "table"
    tpl = templates.get(ex.get("exercise_template_id")) or {}
    p = MUSCLE_PATTERN.get((tpl.get("primary_muscle_group") or "").lower())
    if p:
        return "accessory", p, "muscle"
    n = norm(name)
    p = next((p for rx, p in KEYWORD_PATTERN if re.search(rx, n)), "other")
    return "accessory", p, "name"


def local(ts, tz):
    """Hevy timestamps are ISO with an offset or Z -> naive local datetime."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (dt.astimezone(tz) if dt.tzinfo else dt).replace(tzinfo=None)


def e1rm(kg, reps):
    """Epley; a single is its own 1RM."""
    return kg if reps == 1 else kg * (1 + reps / 30)


def monday(d):
    return (d - timedelta(days=d.weekday())).isoformat()


def lifting_payload(workouts, templates, mapping, garmin_acts, hard_rows, cfg, tz_name):
    lc = cfg.get("lifting", {})
    if not workouts:
        return None
    tz = ZoneInfo(tz_name)
    tpl = {t["id"]: t for t in templates or []}
    to_unit = KG_TO_LB if lc.get("unit", "lb") == "lb" else 1.0
    max_reps = lc.get("e1rm_max_reps", 10)
    prehab_rx = re.compile(rf"\b{re.escape(lc.get('prehab_title', 'prehab'))}\b", re.I)
    heavy_series = set(lc.get("heavy_lower_series", ["Squat", "Deadlift"]))

    sessions, e1 = [], defaultdict(list)
    unmapped = defaultdict(lambda: {"n": 0})
    for w in workouts:
        st, en = local(w.get("start_time"), tz), local(w.get("end_time"), tz)
        if not st:
            continue
        prehab = bool(prehab_rx.search(w.get("title") or ""))
        exs, heavy_sets, best = [], 0, {}
        pattern_sets = defaultdict(int)
        for ex in w.get("exercises") or []:
            series, pattern, how = classify(ex, mapping, tpl)
            if how != "table" and not prehab:
                u = unmapped[ex.get("title") or "?"]
                u.update(pattern=pattern, how=how)
                u["n"] += 1
            sets = ex.get("sets") or []
            hard = [s for s in sets if (s.get("type") or "normal") in HARD_SET_TYPES]
            if not prehab:
                pattern_sets[pattern] += len(hard)
                if series in heavy_series:
                    heavy_sets += len(hard)
            # best qualifying set (normal/failure, 1..max_reps reps, weighted) by e1RM
            top, top_e = None, 0.0
            for s in hard:
                kg, reps = s.get("weight_kg") or 0, s.get("reps") or 0
                if kg > 0 and 1 <= reps <= max_reps and e1rm(kg, reps) > top_e:
                    top, top_e = s, e1rm(kg, reps)
            if top is None and hard:  # bodyweight or timed: show the biggest set for the list
                top = max(hard, key=lambda s: ((s.get("weight_kg") or 0), (s.get("reps") or 0), (s.get("duration_seconds") or 0)))
            rpes = [s["rpe"] for s in hard if s.get("rpe")]
            exs.append({"name": ex.get("title") or "?", "series": series, "pattern": pattern, "hard": len(hard),
                        "top": fmt_set(top, to_unit), "rpe": max(rpes) if rpes else None})
            if top_e and series != "accessory" and not prehab:
                if top_e > best.get(series, (0,))[0]:
                    best[series] = (top_e, top)
        for series, (val, s) in best.items():
            e1[series].append({"date": st.date().isoformat(), "e1rm": round(val * to_unit, 1),
                               "w": round((s.get("weight_kg") or 0) * to_unit, 1), "r": s.get("reps"), "rpe": s.get("rpe")})
        sessions.append({
            "id": w.get("id"), "date": st.date().isoformat(), "start": st.strftime("%H:%M"), "_st": st,
            "_en": en or st, "minutes": round(((en or st) - st).total_seconds() / 60) if en else None,
            "title": w.get("title") or "", "prehab": prehab, "unlogged": False, "exercises": exs,
            "heavy_sets": heavy_sets, "sets": dict(pattern_sets), "hr": None, "load": None,
        })
    sessions.sort(key=lambda s: s["_st"])
    if not sessions:
        return None

    # Garmin strength sessions: HR and load for matched Hevy workouts; unmatched days with no
    # Hevy workout count as "unlogged" lifts so a forgotten entry doesn't zero the week (coach Q9).
    first_day = sessions[0]["date"]
    by_day = defaultdict(list)
    for s in sessions:
        by_day[s["date"]].append(s)
    match_h = lc.get("garmin_match_hours", 3)
    matched = unlogged = 0
    for a in garmin_acts or []:
        if "strength" not in (a.get("type") or "") or not a.get("date"):
            continue
        same = by_day.get(a["date"], [])
        if same:
            target = same[0]
            if a.get("start"):
                gst = datetime.fromisoformat(a["start"])
                near = min(same, key=lambda s: abs((s["_st"] - gst).total_seconds()))
                if abs((near["_st"] - gst).total_seconds()) <= match_h * 3600:
                    target = near
            if target["hr"] is None:
                target["hr"], target["load"] = a.get("hr") and round(a["hr"]), a.get("load") and round(a["load"])
            matched += 1
        elif a["date"] >= first_day and (a.get("minutes") or 0) >= lc.get("unlogged_min_minutes", 10):
            gst = datetime.fromisoformat(a["start"]) if a.get("start") else datetime.fromisoformat(a["date"] + "T12:00")
            sessions.append({"id": f"g-{a['date']}-{a.get('start', '')}", "date": a["date"], "start": a.get("start", "")[11:16] or None,
                             "_st": gst, "_en": gst + timedelta(minutes=a.get("minutes") or 0), "minutes": round(a.get("minutes") or 0),
                             "title": a.get("name") or "Garmin strength", "prehab": False, "unlogged": True, "exercises": [],
                             "heavy_sets": 0, "sets": {}, "hr": a.get("hr") and round(a["hr"]), "load": a.get("load") and round(a["load"])})
            unlogged += 1
    sessions.sort(key=lambda s: s["_st"])

    # Heavy lower body within ~24 h before a hard row (coach Q6): soft heads-up.
    hours, min_sets = lc.get("heavy_lower_hours", 24), lc.get("heavy_lower_min_sets", 3)
    pre_hard = []
    for s in sessions:
        s["before_hard"] = []
        if s["prehab"] or s["heavy_sets"] < min_sets:
            continue
        for r in hard_rows:
            if s["_st"] < r["dt"] and r["dt"] - s["_en"] <= timedelta(hours=hours):
                gap = round((r["dt"] - s["_en"]).total_seconds() / 3600, 1)
                hit = {"lift_date": s["date"], "lift_start": s["start"], "sets": s["heavy_sets"], "row_date": r["dt"].date().isoformat(),
                       "row_start": r["dt"].strftime("%H:%M"), "row": r["label"], "gap_h": max(0, gap)}
                s["before_hard"].append(hit)
                pre_hard.append(hit)

    # Weekly counts, every week from the first Hevy week to now (empty weeks show as zero).
    weeks = {}
    cur, last = date.fromisoformat(monday(date.fromisoformat(first_day))), date.today()
    while cur <= last:
        weeks[cur.isoformat()] = {"week": cur.isoformat(), "lifts": 0, "unlogged": 0, "prehab": 0, "sets": {p: 0 for p in PATTERNS}}
        cur += timedelta(days=7)
    for s in sessions:
        wk = weeks.setdefault(monday(date.fromisoformat(s["date"])),
                              {"week": monday(date.fromisoformat(s["date"])), "lifts": 0, "unlogged": 0, "prehab": 0, "sets": {p: 0 for p in PATTERNS}})
        if s["prehab"]:
            wk["prehab"] += 1
            continue
        wk["lifts"] += 1
        wk["unlogged"] += s["unlogged"]
        for p, n in s["sets"].items():
            if p in wk["sets"]:
                wk["sets"][p] += n

    for s in sessions:
        del s["_st"], s["_en"]
    return {
        "unit": lc.get("unit", "lb"),
        "cfg": {k: v for k, v in lc.items() if not k.startswith("_")},
        "sessions": sessions,
        "e1rm": dict(e1),
        "weeks": [weeks[k] for k in sorted(weeks)],
        "unmapped": sorted(({"name": k, **v} for k, v in unmapped.items()), key=lambda u: -u["n"]),
        "pre_hard": pre_hard,
        "garmin": {"matched": matched, "unlogged": unlogged},
        "first": first_day,
    }


def fmt_set(s, to_unit):
    if not s:
        return ""
    kg, reps, dur = s.get("weight_kg") or 0, s.get("reps"), s.get("duration_seconds")
    if kg and reps:
        return f"{round(kg * to_unit):g} × {reps}"
    if reps:
        return f"BW × {reps}"
    if dur:
        return f"{round(dur)} s"
    return ""
