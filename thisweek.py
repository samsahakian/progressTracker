"""This-week tiles, flags and a plain-text summary, computed once at build time.

The page renders payload["this_week"]; the coach's daily review reads summary.md / data.json.
Both see the same numbers. "Today" is the build date in America/Chicago (config "timezone").
"""
import math
from datetime import date, timedelta


# ---------- formatting (mirrors the page's helpers) ----------
def jround(x):
    """JavaScript Math.round (half up), so numbers match what the page used to show."""
    return math.floor(x + 0.5)


def r100(v):
    return jround(v / 100) * 100


def fmt_m(m):
    m = jround(m)
    return f"{m / 1e6:.2f}M" if m >= 1e6 else f"{jround(m / 1e3)}k" if m >= 1e4 else f"{m:,}"


def fmt_day(d):
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%a %b} {d.day}"


def fmt_split(s):
    if s is None:
        return "—"
    m = int(s // 60)
    return f"{m}:{s - m * 60:04.1f}"


def fmt_time(s):
    if s is None:
        return "—"
    h, m, r = int(s // 3600), int(s % 3600 // 60), s % 60
    return f"{h}:{m:02d}:{r:04.1f}" if h else f"{m}:{r:04.1f}"


def fmt_hm(mins):
    return f"{int(mins // 60)}h{jround(mins % 60):02d}"


def _flag(level, lead, rest):
    return {"level": level, "lead": lead, "text": lead + rest}


# ---------- goal ----------
def goal_stats(g, today):
    start, end = date.fromisoformat(g["start"]), date.fromisoformat(g["end"])
    n = (end - start).days + 1
    status = "upcoming" if today < start else "done" if today > end else "active"
    last = min(today, end)
    erg = water = 0
    d = start
    while d <= last:
        v = g["_daily"].get(d.isoformat(), {})
        erg += v.get("erg", 0)
        if "water" in g.get("sources", []):
            water += v.get("water", 0)
        d += timedelta(days=1)
    done = erg + water
    before = max(0, min(n, (today - start).days))  # whole days before today
    days_left = 0 if status == "done" else n - before
    required = g["target"] * before / n
    return {"days": n, "status": status, "done": done, "erg": erg, "water": water,
            "required": round(required), "ahead": jround(done - required), "days_left": days_left,
            "per_day": round(max(0, (g["target"] - done) / days_left)) if days_left else 0,
            "base_per_day": round(g["target"] / n)}


def current_goal(goals, daily, today):
    gs = [(g, goal_stats({**g, "_daily": daily}, today)) for g in goals]
    if not gs:
        return None
    active = [x for x in gs if x[1]["status"] == "active"]
    upcoming = sorted((x for x in gs if x[1]["status"] == "upcoming"), key=lambda x: x[0]["start"])
    return (active or upcoming or [gs[-1]])[0]


def rolling28(rows, key):
    out = []
    for i, r in enumerate(rows):
        d, s, n, j = date.fromisoformat(r["date"]), 0, 0, i
        while j >= 0 and (d - date.fromisoformat(rows[j]["date"])).days < 28:
            s += rows[j][key]
            n += 1
            j -= 1
        out.append({"date": r["date"], "y": s / n})
    return out


def pre_hard_flag(h):
    if h["kind"] == "same":
        gap = f"{jround(h['gap_h'] * 60)} min" if h["gap_h"] < 1 else f"{h['gap_h']:.1f} h"
        return _flag("", "Lifted right before a hard row",
                     f": {h['sets']} squat/deadlift sets ending {gap} before {h['row'].lower()} on "
                     f"{fmt_day(h['row_date'])} {h['row_start']}. Do the hard erg first, lift after.")
    start = f" {h['lift_start']}" if h.get("lift_start") else ""
    return _flag("", "Heavy legs the day before",
                 f": {h['sets']} squat/deadlift sets on {fmt_day(h['lift_date'])}{start}, {jround(h['gap_h'])} h before "
                 f"{h['row'].lower()} {fmt_day(h['row_date'])} {h['row_start']}. Heads-up only; expect some carryover.")


# ---------- this week ----------
def this_week(p, today, built):
    """Everything the This-week tiles and flags show. Sections without data are None (the page shows "—")."""
    a = p.get("athlete") or {}
    daily = p["daily"]
    monday = today - timedelta(days=today.weekday())
    wk, td = monday.isoformat(), today.isoformat()
    flags = []
    out = {"as_of": {"built": built, "today": td, "data_through": p["summary"].get("last")},
           "week_start": wk}

    # Goal
    goal = None
    cg = current_goal(p.get("goals", []), daily, today)
    if cg:
        g, s = cg
        goal = {"id": g.get("id"), "label": g["label"], "start": g["start"], "end": g["end"], "target": g["target"],
                "sources": g.get("sources", []), **s}
        if s["status"] == "upcoming":
            goal["starts_in"] = in_days = (date.fromisoformat(g["start"]) - today).days
            if in_days <= 7:
                when = "tomorrow" if in_days == 1 else fmt_day(g["start"])
                flags.append(_flag("", f"{g['label']} starts {when}.",
                                   f" {r100(s['base_per_day']):,} m/day for {s['days']} days gets you to {fmt_m(g['target'])}."))
        elif s["status"] == "active":
            ahead = s["ahead"]
            side = f"{abs(ahead):,} m {'ahead' if ahead >= 0 else 'behind'}"
            if ahead < -s["base_per_day"]:
                days = abs(ahead) / s["base_per_day"] if s["base_per_day"] else 0
                flags.append(_flag("warn", f"Behind {g['label']} pace by {abs(ahead):,} m",
                                   f" (about {days:.1f} days). Need {r100(s['per_day']):,} m/day for the last {s['days_left']} days."))
            else:
                flags.append(_flag("good", f"On {g['label']} pace",
                                   f": {fmt_m(s['done'])} of {fmt_m(g['target'])}, {side}. {r100(s['per_day']):,} m/day finishes it."))
            erg_days = [d for d, v in daily.items() if v.get("erg", 0) > 0 and d <= td]
            if erg_days and (today - date.fromisoformat(erg_days[-1])).days >= 3:
                flags.append(_flag("warn", f"No erg meters since {fmt_day(erg_days[-1])}.",
                                   f" Each missed day adds {r100(s['per_day'] / max(1, s['days_left'] - 1)):,} m to every remaining day."))
    out["goal"] = goal

    # Meters and training time
    erg = water = 0
    d = monday
    while d <= today:
        v = daily.get(d.isoformat(), {})
        erg += v.get("erg", 0)
        water += v.get("water", 0)
        d += timedelta(days=1)
    tw = next((x for x in p.get("time_weeks", []) if x["week"] == wk), None)
    out["meters"] = {"erg": erg, "water": water, "has_water": bool(p.get("water")),
                     "minutes": (tw["erg"] + tw["water"] + tw["indoor"]) if tw else 0}

    # Hard sessions
    hw = next((x for x in p.get("hard_weeks", []) if x["week"] == wk), None)
    out["hard"] = {"n": hw["n"] if hw else 0, "target": a.get("hard_sessions_target", 2),
                   "erg": hw["erg"] if hw else 0, "water": hw["water"] if hw else 0, "indoor": hw["indoor"] if hw else 0}

    # Intensity (% of time in zone)
    iw = next((x for x in p.get("intensity", []) if x["week"] == wk), None)
    tot = (iw["easy"] + iw["moderate"] + iw["hard"]) if iw else 0
    if tot:
        ip = {"easy": 100 * iw["easy"] / tot, "z3": 100 * iw["moderate"] / tot, "hard": 100 * iw["hard"] / tot,
              "minutes": jround(tot / 60), "includes_water": bool(iw.get("water_s"))}
        out["intensity"] = {k: round(v, 1) if isinstance(v, float) else v for k, v in ip.items()}
        z3_lim, easy_t = a.get("z3_flag_pct", 15), a.get("easy_target_pct", 80)
        hr_top = (a.get("efficiency_hr_band") or [142, 149])[1]
        if ip["z3"] > z3_lim:
            flags.append(_flag("warn", f"Z3 is {jround(ip['z3'])}% of erg time this week",
                               f" (limit {z3_lim}%). Keep steady pieces at or below {hr_top} bpm."))
        elif ip["easy"] < easy_t and tot >= 3600:
            flags.append(_flag("warn", f"{jround(ip['easy'])}% easy this week", f", under the {easy_t}% target."))
    else:
        out["intensity"] = None
    out["intensity_na"] = None if tot else ("no erg HR this week yet" if p["strokes_coverage"][0] else "needs per-stroke HR")

    # HR drift
    lim = a.get("drift_flag_pct", 5)
    wd = [r for r in p.get("drift", []) if r["date"] >= wk and r["drift"] > lim]
    out["drift"] = [{"date": r["date"], "drift": r["drift"]} for r in wd]
    if wd:
        flags.append(_flag("warn", "HR drift " + ", ".join(f"{r['drift']:.1f}%" for r in wd),
                           f" on {'a steady session' if len(wd) == 1 else 'steady sessions'} this week (flag at {lim}%). "
                           "Check fueling, heat and sleep, or ease the pace."))

    # Pace at the efficiency HR band
    hb = a.get("efficiency_hr_band") or [142, 149]
    rows = sorted((r for r in p.get("efficiency", []) if hb[0] <= r["hr"] <= hb[1] + 0.99), key=lambda r: r["date"])
    roll = rolling28(rows, "split")
    eff = None
    if roll:
        last = roll[-1]
        ld = date.fromisoformat(last["date"])
        prev = next((x for x in reversed(roll) if (ld - date.fromisoformat(x["date"])).days >= 28), None)
        eff = {"band": hb, "split": round(last["y"], 1), "date": last["date"], "sessions": len(rows),
               "prev_split": round(prev["y"], 1) if prev else None, "prev_date": prev["date"] if prev else None,
               "direction": None if not prev else "faster" if last["y"] <= prev["y"] else "slower"}
    out["efficiency"] = eff

    # Resting HR
    rec = p.get("recovery") or []
    rl = rec[-1] if rec else None
    rhr = None
    if rl and rl.get("rhr7"):
        streak = 0
        for r in reversed(rec):
            if not r["high"]:
                break
            streak += 1
        rhr = {"date": rl["date"], "rhr": rl["rhr"], "rhr7": rl["rhr7"], "base": rl["base"],
               "diff": round(rl["rhr7"] - rl["base"], 1) if rl["base"] else None,
               "sleep7": rl.get("sleep7"), "streak": streak}
        if streak >= a.get("rhr_flag_days", 3):
            flags.append(_flag("warn", f"Resting HR {a.get('rhr_flag_bpm', 4)}+ bpm over baseline for {streak} days",
                               f" ({rl['rhr']} today vs {rl['base']}). Consider an easier day."))
    out["rhr"] = rhr

    # Lifts · Prehab
    lift = p.get("lifting")
    lw = None
    if lift:
        c = lift.get("cfg") or {}
        w = next((x for x in lift["weeks"] if x["week"] == wk), None)
        done = [s for s in lift["sessions"] if not s["prehab"] and s["date"] <= td]
        pre = [h for h in lift["pre_hard"] if wk <= h["row_date"] <= td]
        lw = {"lifts": w["lifts"] if w else 0, "lifts_target": c.get("lifts_target", 2),
              "prehab": w["prehab"] if w else 0, "prehab_target": c.get("prehab_target", 3),
              "unlogged": w["unlogged"] if w else 0,
              "last_lift": done[-1]["date"] if done else None,
              "days_since": (today - date.fromisoformat(done[-1]["date"])).days if done else None,
              "pre_hard": pre}
        flags.extend(pre_hard_flag(h) for h in pre)
    out["lifts"] = lw

    out["flags"] = flags
    return out


# ---------- summary.md ----------
def summary_md(p):
    tw = p["this_week"]
    a = tw["as_of"]
    L = [f"Rowing Progress, built {a['built']} CT · data through {fmt_day(a['data_through']) if a['data_through'] else '—'}",
         f"Week of {fmt_day(tw['week_start'])} (through {fmt_day(a['today'])})"]

    g = tw["goal"]
    if g:
        if g["status"] == "upcoming":
            L.append(f"{g['label']}: upcoming, starts {fmt_day(g['start'])} (in {g['starts_in']} days) · "
                     f"{g['target']:,} m in {g['days']} days · {g['base_per_day']:,} m/day")
        elif g["status"] == "active":
            src = f"erg {g['erg']:,}" + (f" + water {g['water']:,}" if "water" in g["sources"] else "")
            L.append(f"{g['label']}: active, ends {fmt_day(g['end'])} · {g['done']:,} of {g['target']:,} m ({src}) · "
                     f"required to date {g['required']:,} · {abs(g['ahead']):,} m {'ahead' if g['ahead'] >= 0 else 'behind'} · "
                     f"{g['per_day']:,} m/day to finish · {g['days_left']} days left (base {g['base_per_day']:,} m/day)")
        else:
            L.append(f"{g['label']}: {'done' if g['done'] >= g['target'] else 'ended'} {fmt_day(g['end'])} · "
                     f"{g['done']:,} of {g['target']:,} m")
    else:
        L.append("Goal: none set")

    m = tw["meters"]
    L.append(f"Meters: {m['erg']:,} erg · {m['water']:,} water · {fmt_hm(m['minutes'])} training")
    h = tw["hard"]
    L.append(f"Hard sessions: {h['n']}/{h['target']} (erg {h['erg']} · water {h['water']} · Garmin indoor {h['indoor']})")
    i = tw["intensity"]
    L.append(f"Intensity: {jround(i['easy'])}% easy · Z3 {jround(i['z3'])}% · hard {jround(i['hard'])}% · {i['minutes']} min"
             f"{' incl. water' if i['includes_water'] else ' (erg only)'}" if i else f"Intensity: n/a ({tw['intensity_na']})")
    e = tw["efficiency"]
    if e:
        band = f"{e['band'][0]}–{e['band'][1]} bpm"
        cmp_ = (f" · {e['direction']} than {fmt_split(e['prev_split'])} four weeks earlier ({fmt_day(e['prev_date'])})"
                if e["prev_split"] is not None else " · no value 28+ days earlier")
        L.append(f"Pace at {band}: {fmt_split(e['split'])} (4-wk avg to {fmt_day(e['date'])}){cmp_}")
    else:
        L.append("Pace at efficiency band: n/a (no steady sessions in the HR band yet)")
    r = tw["rhr"]
    if r:
        base = f"baseline {r['base']}, {r['diff']:+.1f}" if r["base"] else "baseline building"
        sleep = f" · sleep {r['sleep7']:.1f} h" if r["sleep7"] else ""
        L.append(f"Resting HR 7d: {r['rhr7']} ({base}){sleep} · streak {r['streak']} · latest {fmt_day(r['date'])}")
    else:
        L.append("Resting HR 7d: n/a (needs Garmin daily data)")
    lw = tw["lifts"]
    if lw:
        last = f"last lift {fmt_day(lw['last_lift'])} ({lw['days_since']} d ago)" if lw["last_lift"] else "no lifts yet"
        L.append(f"Lifts {lw['lifts']}/{lw['lifts_target']} · Prehab {lw['prehab']}/{lw['prehab_target']} · {last}"
                 + (f" (incl. {lw['unlogged']} Garmin-only, not in Hevy)" if lw["unlogged"] else ""))
    else:
        L.append("Lifts: n/a (no Hevy data)")

    L.append("Flags:" if tw["flags"] else "Flags: none")
    L += [f"- [{f['level'] or 'info'}] {f['text']}" for f in tw["flags"]]

    L += ["Last 10 sessions (newest first):", "date | start | source | type | meters | time | split | rate | avg HR"]
    ss = sorted(p.get("sessions", []), key=lambda s: (s["date"], s.get("start") or ""))[-10:]
    for s in reversed(ss):
        L.append(" | ".join(str(x) for x in (
            s["date"], s.get("start") or "—", s["kind"], s["type"] + (" (short)" if s.get("short") else ""),
            f"{s['meters']:,}" if s.get("meters") is not None else "—", fmt_time(s.get("seconds")),
            fmt_split(s.get("split")), s.get("rate") or "—", round(s["hr"]) if s.get("hr") else "—")))

    L += ["Last 3 benchmark pieces (any format, newest first):", "date | format | split | HR | W/HR"]
    bench = sorted(((r, b["label"]) for b in p.get("benchmarks", []) for r in b["rows"]),
                   key=lambda x: (x[0]["date"], x[0].get("start_t") or ""))[-3:]
    for r_, lbl in reversed(bench):
        L.append(" | ".join(str(x) for x in (
            r_["date"], lbl, fmt_split(r_.get("split")), r_.get("hr") or "—",
            f"{r_['w_hr']:.3f}" if r_.get("w_hr") else "—")))
    return "\n".join(L) + "\n"
