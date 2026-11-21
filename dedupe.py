"""Match Garmin indoor-rowing activities to Concept2 Logbook results (coach rule B4).

A Garmin session matches a Logbook session on the same date when the start times are within 10 minutes
and the durations within 10% (Logbook duration = work + rest). Garmin sessions with no match are
"Garmin-only" (e.g. club practices that were never logged to Concept2).
"""
from datetime import datetime

START_TOL_S = 10 * 60
DUR_TOL = 0.10


def _dt(s):
    try:
        return datetime.fromisoformat(s.replace("T", " ")[:19])
    except (ValueError, AttributeError):
        return None


def logbook_sessions(results):
    out = []
    for r in results:
        start = _dt(r.get("date") or "")
        secs = (r.get("time") or 0) / 10
        if not start or secs <= 0:
            continue
        rest = sum((iv.get("rest_time") or 0) / 10 for iv in ((r.get("workout") or {}).get("intervals") or []))
        out.append((r["id"], start, secs + rest))
    return out


def match(indoor, results):
    """{garmin_id: logbook_id or None} plus a count of how each match was made."""
    logs = logbook_sessions(results)
    by_day = {}
    for lid, start, dur in logs:
        by_day.setdefault(start.date(), []).append((lid, start, dur))
    out, how, kind_of = {}, {"start": 0, "end": 0}, {}
    for g in indoor:
        gs, gdur = _dt(g.get("start") or ""), g.get("elapsed") or g.get("seconds") or 0
        found = None
        if gs and gdur:
            for lid, ls, ldur in by_day.get(gs.date(), []):
                if abs(gdur - ldur) > DUR_TOL * max(gdur, ldur):
                    continue
                if abs((gs - ls).total_seconds()) <= START_TOL_S:
                    found, kind = lid, "start"
                    break
                # some Logbook times are the finish time; allow that too
                if abs((gs - ls).total_seconds() + gdur) <= START_TOL_S:
                    found, kind = lid, "end"
                    break
            if found:
                how[kind] += 1
                kind_of[found] = kind
        out[g["id"]] = found
    match.kind_of = kind_of  # Logbook id -> "start" | "end", for sessions with a Garmin twin
    return out, how
