# Portfolio MRI

Monthly one-page portfolio diagnostics: what is working, what is not, and how consistent the investing has been. Zero personal data in this repo by design.

## What it is

Portfolio analysis as a data problem: every rupee of return decomposes into allocation, selection (vs benchmark), and timing. The dashboard shows:

- Headline: portfolio value, lifetime P&L, return on cost
- Allocation and return by sleeve (direct equity, gold, index MF, active MF, ELSS, hybrid, FOF)
- Contribution matrix: top gainers and losers by INR contribution, win rate
- Concentration: positions over 5% of a sleeve, effective N (1/sum(w^2))
- Stress test: simulated equity -25%, gold flat
- Investment discipline grade: trailing 12 months, weighted components (contributions 35%, holding 25%, rebalancing 20%, risk 20%), graded A-F. Scores only ever apply to a prospectively recorded policy, never retroactively.
- Data readiness: which analyses are live vs need CAS / full trade history

## Repo layout

- `mr.html` — the dashboard, single self-contained page. Fetches `mr-data.json`; if absent, falls back to `sample-data.json` so the page renders on GitHub Pages.
- `gen_mri_data.py` — local generator. Reads holdings snapshots (Zerodha/Kite style JSON: equity + mutual funds) and writes `mr-data.json` for the dashboard. Contains no portfolio numbers of its own.
- `sample-data.json` — anonymous demo data matching the schema. All names are EXAMPLE / SAMPLE placeholders.
- `policy.example.json` — template for the discipline policy that activates the grade.

## Run it yourself

1. Clone. Put your holdings in the expected shape (see `gen_mri_data.py` header / class functions).
2. `python3 gen_mri_data.py` produces `mr-data.json`.
3. Serve `mr.html` + `mr-data.json` anywhere. Keep the data file private; the data is personal.

GitHub Pages demo: rename `sample-data.json` to `mr-data.json`, or just open `mr.html` (it falls back automatically).

## Generating discipline grades

The grade only activates after you record a policy: contribution schedule (amounts/dates), holding/exit rules, allocation bands, quarterly review cadence, risk limits. Copy `policy.example.json` and fill in `score_rate` per component (0-10, trailing 12-month compliance ratio). Missing policy shows "needs-policy", never a fabricated grade.

## Policy & goals editor (in-page)

`mr.html` includes a Policy & Goals editor panel (hosted deployments only). It saves to `policy.json` next to the dashboard via a small loopback API (`mri_api.py`, systemd `mri-api.service`, port 8302) proxied by nginx at `/mri-api/` behind the site's basic auth:

- `GET /mri-api/policy` — current policy
- `POST /mri-api/policy` — validate + save (JSON object; known top-level keys only)
- `POST /mri-api/refresh` — re-run `gen_mri_data.py` from the latest holdings snapshots

Goals are dated required-corpus targets mapped to sleeves: `{"goals": [{"name", "target_inr", "target_date", "sleeves": [...]}]}`. When present, the Goal funding panel shows funded % per goal (current market value of linked sleeves vs target — a snapshot, not a forecast). `policy.json` is gitignored; it can hold personal targets.

## Privacy by design

- The dashboard code ships with no personal numbers.
- The generator contains no personal data; it only reads local files.
- The live data file is produced locally and must be served behind auth (see nginx `auth_basic` in the deployment notes) or kept off the public repo entirely.
- Held positions, amounts, fund names and folios are personal data; this public repo intentionally omits them.
## Rebuild (Sep 2026)

Dashboard rebuilt with the OpenAI `build-web-data-visualization` Codex plugin: SVG allocation + diverging contribution charts, stat strip with XIRR, behavioral ledger, concentration, stress, discipline, goals, data readiness. Mobile-first, dark mode, reduced-motion, print styles, skip-link, no external assets. Data contract unchanged: `mr.html` fetches `mr-data.json` at runtime and falls back to `sample-data.json`.
