# c2-progress

Pulls your Concept2 Logbook and builds a dashboard of progress over time:

- **Meters** per week and cumulative by season (May 1 – Apr 30)
- **Benchmarks** — 30r20, 1k, 2k, 5k, 6k and 30-min tests: split, watts, HR and W/HR per piece, with running PBs marked
- **Steady-state efficiency** — watts per heartbeat on continuous low-rate pieces, with a rolling average

Python standard library only; nothing to install.

## Get a token

log.concept2.com → **Settings → Developer** → create a personal access token.

## Run it locally

```bash
export C2_TOKEN=your_token
python3 sync.py        # writes data/results.json (rower only; --machine all for SkiErg/BikeErg too)
python3 build.py       # writes site/index.html — open it in a browser
```

Re-run `sync.py --since 2026-11-01` to fetch only recent workouts; it merges with what's on disk.

To try it without a token: `python3 tools/make_sample.py && python3 build.py --data data/sample.json`.

## Run it on GitHub (daily refresh)

Uses a Concept2 API key (client ID + secret, platform "Other",
callback `http://localhost:8080/callback`, no webhook).

1. **Repo secrets** (Settings → Secrets and variables → Actions): `C2_CLIENT_ID` and
   `C2_CLIENT_SECRET`, from the Concept2 API key.

   Concept2 issues a new refresh token every time one is used. The workflows keep it on the
   `c2-state` branch, AES-256 encrypted with `C2_CLIENT_SECRET` as the key, so nothing usable is
   stored in the repo. Don't delete that branch.
2. **Approve access once**: open this in a browser (put in your client ID), approve, and copy the
   `localhost:8080/callback?code=…` address you land on (the page itself won't load — that's fine):

   `https://log.concept2.com/oauth/authorize?client_id=YOUR_CLIENT_ID&scope=user:read,results:read&response_type=code&redirect_uri=http://localhost:8080/callback`

3. Within a few minutes: **Actions → Connect Concept2 → Run workflow**, paste that address.
4. **Settings → Pages → Source: GitHub Actions**.
5. **Actions → Update dashboard → Run workflow** for the first build. After that it runs every morning.

If the daily run ever fails at "Refresh Concept2 token", redo steps 2–3.

Workout data is never committed (data/ and site/ are git-ignored); it's fetched fresh on each run.
**The Pages site itself is public**, though — anyone with the URL can see the dashboard. If that matters,
skip Pages and run it locally instead.

## Tuning what counts as a benchmark

Rules live in `config.json`. A piece matches when it's a single continuous piece of the right distance or
time (±5 s), within the rate limits, and — for 30r20 — at least 12% more watts than your median easy piece
over the prior 60 days, so 30-minute UT2 rows don't get counted.

Override per workout with a Logbook comment:

- `#30r20`, `#2k`, etc. — force a piece into that benchmark (works for interval-programmed pieces too)
- `#notest` — keep a piece out of every benchmark (e.g. a 2k warmup)

Add your own benchmark by appending a rule, e.g. `{ "id": "60min", "label": "60 min", "minutes": 60, "min_effort": 1.05 }`.

## How the numbers are derived

- Split = time ÷ meters × 500
- Watts = 2.80 ÷ (split ÷ 500)³ — Concept2's formula
- W/HR = watts ÷ average heart rate (only when the Logbook has HR for that piece)
