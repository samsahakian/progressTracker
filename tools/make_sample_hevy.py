#!/usr/bin/env python3
"""Write fake Hevy workouts (and Garmin strength sessions) so the Lifting tab can be tried without a key.

python tools/make_sample.py && python tools/make_sample_hevy.py && python build.py --data data/sample.json
"""
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

random.seed(11)
LB = 1 / 2.2046226218
DATA = Path(__file__).resolve().parent.parent / "data"
TPL = {"sq": ("Squat (Barbell)", "quadriceps"), "dl": ("Deadlift (Barbell)", "hamstrings"), "bp": ("Bench Press (Barbell)", "chest"),
       "ohp": ("Overhead Press (Barbell)", "shoulders"), "row": ("Bent Over Row (Barbell)", "upper_back"), "pu": ("Pull Up", "lats"),
       "pl": ("Plank", "abdominals"), "wc": ("Cable Woodchop", "obliques"), "bw": ("Lateral Band Walk", "abductors"),
       "sp": ("Side Plank", "abdominals"), "gb": ("Single Leg Glute Bridge", "glutes"), "pp": ("Pallof Press", "abdominals")}


def sets(lb, reps, n, warm=True):
    out = [{"type": "warmup", "weight_kg": 45 * LB, "reps": 5}] if warm else []
    out += [{"type": "normal", "weight_kg": lb * LB if lb else None, "reps": reps, "rpe": random.choice([None, 7, 8, 8.5])} for _ in range(n)]
    return out


def ex(k, s):
    return {"title": TPL[k][0], "exercise_template_id": k, "sets": s}


workouts, acts = [], []
start, end = date(2026, 6, 1), date(2026, 11, 20)
sq, dl, bp, ohp = 155, 205, 135, 85
d, a = start, True
while d <= end:
    if d.weekday() in (0, 2) and not (date(2026, 8, 10) <= d <= date(2026, 8, 24)) and random.random() < 0.9:
        if d == date(2026, 8, 27):
            dl = 155  # back injury: hinge reset
        t0 = datetime(d.year, d.month, d.day, 18 if d.weekday() == 2 else 5, 30)
        exs = [ex("sq", sets(sq, 5, 3))]
        if a:
            exs += [ex("bp", sets(bp, 5, 3)), ex("dl", sets(dl, 5, 1)), ex("pl", [{"type": "normal", "duration_seconds": 45}] * 2)]
            bp += 5
            dl += 5 if d < date(2026, 8, 1) or d > date(2026, 9, 10) else 0
        else:
            exs += [ex("ohp", sets(ohp, 5, 3)), ex("row", sets(95, 8, 3, warm=False)), ex("pu", [{"type": "normal", "reps": 6}] * 2)]
            if random.random() < 0.4:
                exs.append(ex("wc", sets(30, 10, 2, warm=False)))
            ohp += 2.5
        sq += 5 if d < date(2026, 11, 1) else 0
        mins = random.randint(45, 65)
        workouts.append({"id": f"w{len(workouts)}", "title": "Workout A" if a else "Workout B",
                         "start_time": (t0 + timedelta(hours=6)).isoformat() + "Z", "end_time": (t0 + timedelta(hours=6, minutes=mins)).isoformat() + "Z",
                         "exercises": exs})
        acts.append({"date": d.isoformat(), "type": "strength_training", "load": random.randint(20, 45), "minutes": mins,
                     "start": t0.strftime("%Y-%m-%dT%H:%M"), "hr": random.randint(95, 115), "name": "Strength"})
        a = not a
    elif d.weekday() in (1, 4, 6) and random.random() < 0.75:
        t0 = datetime(d.year, d.month, d.day, 20, 0)
        workouts.append({"id": f"p{len(workouts)}", "title": "Prehab", "start_time": (t0 + timedelta(hours=6)).isoformat() + "Z",
                         "end_time": (t0 + timedelta(hours=6, minutes=15)).isoformat() + "Z",
                         "exercises": [ex("bw", [{"type": "normal", "reps": 15}] * 2), ex("sp", [{"type": "normal", "duration_seconds": 30}] * 2),
                                       ex("gb", [{"type": "normal", "reps": 10}] * 2), ex("pp", sets(20, 10, 2, warm=False))]})
    d += timedelta(days=1)
acts.append({"date": "2026-11-12", "type": "strength_training", "load": 30, "minutes": 40, "start": "2026-11-12T05:30", "hr": 104, "name": "Strength"})

DATA.mkdir(exist_ok=True)
workouts.sort(key=lambda w: w["start_time"])
(DATA / "hevy_workouts.json").write_text(json.dumps(workouts))
(DATA / "hevy_templates.json").write_text(json.dumps([{"id": k, "title": v[0], "primary_muscle_group": v[1]} for k, v in TPL.items()]))
(DATA / "garmin_activities.json").write_text(json.dumps(acts))
print(f"Wrote {len(workouts)} fake Hevy workouts and {len(acts)} Garmin strength sessions to {DATA}")
