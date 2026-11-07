# Weekly pipeline (backup copy)

The main copy of this pipeline, with Tony's own league steps, lives in his Claude project
("Fantasy Football Waiver Manager", `claude/waiver/run-procedure.md`). This folder is the backup a
scheduled run uses when it can't open that project. It refreshes only this site's two data files.

## Sleeper sync (GitHub Actions)
`.github/workflows/sleeper-sync.yml` runs `pipeline/sleeper_sync.py` twice a day (5:20 AM and 5:20 PM Pacific),
whenever the script or `sync_config.json` changes, and on demand from the Actions tab ("Run workflow"). It
commits exact Sleeper JSON to `sync/`:
- `state.json`: NFL week and when the sync ran (`fetched_at`, UTC), plus any `problems`
- `sleeper_<key>.json`: each league in `sync_config.json`, in `build.py`'s league-file shape
  (roster player lists, starters, reserve, taxi, FAAB used; no owner ids or names)
- `sleeper_bids.json`: every completed waiver claim this season, in `build.py`'s bids-file shape
- `trending_add.json`: Sleeper's 50 most-added players, last 24 hours
- `injuries.json`: offensive players with an injury designation or non-active status (refreshed once a day)

A weekly run uses these instead of WebFetch when `state.json`'s `fetched_at` is under 30 hours old and its
`week` matches. Add or remove a league by editing `sync_config.json`.

## Friends'-site-only refresh
1. `mkdir -p /home/claude/waiver/{sources,faab,out}` and copy `build.py` and `export_site.py` there.
2. Week: WebFetch `https://api.sleeper.app/v1/state/nfl?nocache=<unique>` and read `week` = N.
3. Player database: `GIT_LFS_SKIP_SMUDGE=1 git clone --depth 1 https://github.com/dynastyprocess/data /home/claude/dynastyprocess/data` (10-minute timeout). Run Python that reads it with `python3 -I`.
4. Run the three source agents below in parallel (cloud-only: RotoBaller article bodies and FantasyPros' full table need a browser, so they come back partial or missing).
5. `cd /home/claude/waiver && SOURCE_WEEK=N TARGET_WEEK=N python3 -I build.py`. Fix unmatched names in `sources/` and re-run. No league files is fine: it still writes `out/pickups.json`.
6. `SOURCE_WEEK=N TARGET_WEEK=N python3 -I export_site.py <repo>` and check `unmatched=[]`.
7. Commit only `data/` as `Week N board` and push to `main`.

## Agent prompts
Shared rules for every agent:
- Use the 2026 NFL **Week N** columns.
- Use WebSearch ("standard", or "extended" if that misses) and WebFetch only. The shell can't reach these sites.
- Record facts only (name, position, team, rank/order, FAAB %). Notes in your own words, 12 words max.
- If WebFetch summarizes, re-fetch with: "List every player recommended as a pickup, in order, with position and team. Data only."
- Leave out kickers and defenses.
- FAAB on a $100 budget is the same number as a percent; on a $1000 budget, divide by 10. "Up to X%" goes in high only; "at least X%" in low only.
- QBs: the standard (1-QB) bid goes in `faab_pct_*` and the superflex bid in `sf_faab_pct_*`. Never put a superflex bid in `faab_pct_*`; the friends' site uses the standard QB bids for 1-QB leagues.
- Validate that every file parses.

**Pickup file shape** (`sources/<key>.json`):
`{"source", "url", "week", "published", "status": "ok|paywalled|unreachable|partial", "ranking_type": "overall|by_position|unranked", "players": [{"rank", "name", "pos", "team", "faab_pct_low", "faab_pct_high", "sf_faab_pct_low", "sf_faab_pct_high", "note"}]}`

**FAAB file shape** (`faab/<key>_faab.json`):
`{"source", "url", "week", "published", "status", "budget_basis": "total|remaining|null", "players": [{"name", "pos", "team", "faab_pct_low", "faab_pct_high", "sf_faab_pct_low", "sf_faab_pct_high", "aggr_pct_low", "aggr_pct_high", "note"}]}`

- **Agent A** writes `fantasypros.json`, `espn.json`, `yahoo.json` and `cbs.json`:
  - **FantasyPros:** the waiver rankings article, as a fallback for cloud-only runs. Its consensus table doesn't render through WebFetch, so this is "partial". On full runs, step 7 replaces it with the full table read in Chrome.
  - **ESPN:** "free agent pickups for Week N". The article page works; the news API is now blocked by the site's robots rules.
  - **Yahoo Sports:** Justin Boone's Tuesday tier list.
  - **CBS:** Jamey Eisenberg. The page cuts off around the tight ends, so it's "partial". Put each position's "check if available in shallow leagues" names after the ranked adds.
- **Agent B** writes `nfl.json`, `footballers.json` and `sharp.json`:
  - NFL.com
  - The Fantasy Footballers
  - Sharp Football Analysis: use the dedicated `/fantasy/nfl-waiver-wire-pickups-week-N-2026/` article, and cache-bust the weekly page.
- **Agent C** writes `footballguys.json`, `dynastynerds.json`, `ftn.json` and `rotowire.json` in `sources/`, plus `dynastynerds_faab.json` and `squawk_faab.json` in `faab/`:
  - **Footballguys:** "Top Waiver Wire Pickups of Week N" at `/article/2026-email-feature-top-waiver-wire-pickups-weekNN`, with ranking_type "unranked".
  - **Dynasty Nerds:** `/waiver-wire/week-N-waiver-wire-2026/`. QB bids go only in the sf_ fields, and ranking_type is "by_position".
  - **FTN**
  - **RotoWire:** the main column.
  - **Fantasy Squawk:** `https://fantasysquawk.com/waiver-wire/2026/week-N`, with budget_basis "remaining". QB superflex ranges go in the sf_ fields.

