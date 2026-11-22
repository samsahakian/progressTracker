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

Each build also writes `site/data.json` (the page's data) and `site/summary.md` (a plain-text
This-week summary, flags, last 10 sessions and last 3 benchmark pieces). Both deploy with the page
(`…/summary.md`, `…/data.json`), and each run force-pushes them with `index.html` to the
`dashboard-data` branch (one commit) for readers that can clone but can't open github.io. The
This-week tiles and flags are computed in `thisweek.py` as of the build date in Chicago.

## Add on-water sessions from Garmin (optional)

Pulls outdoor rowing activities from Garmin Connect and shows them alongside the erg: separate
weekly bars, an on-water HR/rate/split chart, and a combined session log. Erg and water meters are
kept separate, never summed; season meters stay erg-only, matching Concept2.

1. Garmin Connect on the web → **Settings → Personal API Key → Create Key**. Pick the longest expiry
   (up to a year). It's shown once, so copy it straight away.
2. Repo secret `GARMIN_API_KEY` = that key.
3. **Actions → Update dashboard → Run workflow.**

Indoor rowing activities are skipped, so erg pieces aren't counted twice. When the key expires the
daily run still builds the erg dashboard and shows a warning; make a new key and update the secret.

## Add lifting from Hevy (optional)

Adds a **Lifting** tab (adherence, key-lift e1RM, weekly hard sets by movement pattern, prehab,
session list) and lift-day ticks on the rowing charts. Needs Hevy Pro for the API.

1. Hevy → **Settings → Developer** (or hevy.com/settings?developer) → generate an API key.
2. Repo secret `HEVY_API_KEY` = that key.
3. **Actions → Update dashboard → Run workflow.**

Exercises are grouped by `lifts.csv` (Hevy exercise name → series, movement pattern). Anything not
listed there is an accessory, with its pattern guessed from Hevy's muscle group; the Lifting tab lists
those so you can add a row. Log prehab as a Hevy workout titled "Prehab". Weight unit and targets are
under `lifting` in `config.json`.
