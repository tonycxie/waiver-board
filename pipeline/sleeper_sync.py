"""Pull exact Sleeper JSON for Tony's waiver run. Runs in GitHub Actions (.github/workflows/sleeper-sync.yml).

The Claude workspace that builds the waiver board can't reach api.sleeper.app, and WebFetch has served
weeks-old copies of it. This runs where the network is open and commits plain JSON into sync/, which the
weekly run reads with `git pull`.

Writes (only fields the board uses; no owner ids, user names or chat):
  sync/state.json              NFL state (season, week, leg) and when this ran
  sync/sleeper_<key>.json      one per league in pipeline/sync_config.json, in build.py's league-file shape
  sync/sleeper_bids.json       completed waiver claims this season, in build.py's bids-file shape
  sync/trending_add.json       Sleeper's most-added players, last 24 hours
  sync/injuries.json           offensive players with an injury designation or a non-active status
                               (from /players/nfl, which Sleeper asks to call at most once a day)

Usage: python3 pipeline/sleeper_sync.py [out_dir]     (stdlib only; SLEEPER_API overrides the base URL)
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

API = os.environ.get("SLEEPER_API", "https://api.sleeper.app/v1").rstrip("/")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "sync")
CFG = json.load(open(os.environ.get("SYNC_CONFIG", os.path.join(HERE, "sync_config.json"))))
OFFENSE = {"QB", "RB", "WR", "TE"}
NOW = datetime.now(timezone.utc)
STAMP = NOW.isoformat(timespec="seconds")


def get(path, tries=4):
    url = API + path
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "waiver-board-sync/1.0",
                                                       "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # network blips, 429s, 5xx
            if i == tries - 1:
                raise RuntimeError(f"{url}: {e}") from e
            time.sleep(2 * (i + 1))


def write(name, data):
    os.makedirs(OUT, exist_ok=True)
    tmp = os.path.join(OUT, name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, os.path.join(OUT, name))


def my_roster(rosters, user_id):
    for r in rosters:
        if r.get("owner_id") == user_id or user_id in (r.get("co_owners") or []):
            return r
    return None


state = get("/state/nfl")
week = int(state.get("leg") or state.get("week") or 1)
thru = max(1, min(18, week))
problems = []

bids = {"fetched_at": STAMP, "leagues": {}}
leagues_written = []
for c in CFG["leagues"]:
    lid, key = c["league_id"], c["key"]
    try:
        L = get(f"/league/{lid}")
        rosters = get(f"/league/{lid}/rosters")
    except RuntimeError as e:
        problems.append(str(e))
        continue
    s = L.get("settings") or {}
    mine = my_roster(rosters, CFG["user_id"])
    if not mine:
        problems.append(f"{key}: no roster owned by user {CFG['user_id']}")
        continue
    out = {
        "league_id": lid, "name": L.get("name"), "treat_as": c["treat_as"],
        "budget": int(s.get("waiver_budget") or 100), "teams": int(s.get("num_teams") or len(rosters)),
        "previous_league_id": L.get("previous_league_id"),
        "roster_positions": L.get("roster_positions") or [],
        "rec": (L.get("scoring_settings") or {}).get("rec"),
        "waiver_type": s.get("waiver_type"), "league_type": s.get("type"),
        "my_roster_id": mine["roster_id"],
        "rosters": {
            str(r["roster_id"]): {
                "budget_used": int((r.get("settings") or {}).get("waiver_budget_used") or 0),
                "players": sorted(r.get("players") or []),
                "starters": r.get("starters") or [],
                "reserve": sorted(r.get("reserve") or []),
                "taxi": sorted(r.get("taxi") or []),
            } for r in sorted(rosters, key=lambda r: r["roster_id"])
        },
        "season": L.get("season"), "week": week, "fetched_at": STAMP, "source": "sleeper-sync",
    }
    write(f"sleeper_{key}.json", out)
    leagues_written.append(key)

    claims = []
    for w in range(1, thru + 1):
        try:
            txs = get(f"/league/{lid}/transactions/{w}") or []
        except RuntimeError as e:
            problems.append(str(e))
            continue
        for t in txs:
            if t.get("type") != "waiver" or t.get("status") != "complete":
                continue
            for pid, rid in (t.get("adds") or {}).items():
                claims.append({"week": int(t.get("leg") or w), "player_id": pid,
                               "bid": (t.get("settings") or {}).get("waiver_bid"),
                               "roster_id": rid, "created": t.get("created")})
    claims.sort(key=lambda x: (x["week"], -(x["created"] or 0)))
    bids["leagues"][lid] = {"name": L.get("name"), "season": int(L.get("season") or state.get("season") or 0),
                            "budget": out["budget"], "waiver_type": s.get("waiver_type"), "claims": claims}

write("sleeper_bids.json", bids)

try:
    trend = get("/players/nfl/trending/add?lookback_hours=24&limit=50")
    write("trending_add.json", {"fetched_at": STAMP, "lookback_hours": 24, "players": trend})
except RuntimeError as e:
    problems.append(str(e))

# /players/nfl is ~5 MB; Sleeper asks for at most one call a day.
inj_path = os.path.join(OUT, "injuries.json")
last = None
if os.path.exists(inj_path):
    try:
        last = json.load(open(inj_path)).get("fetched_at", "")[:10]
    except ValueError:
        last = None
if last != NOW.date().isoformat() or os.environ.get("FORCE_PLAYERS"):
    try:
        allp = get("/players/nfl")
        flagged = {}
        for pid, p in allp.items():
            if p.get("position") not in OFFENSE or not p.get("team"):
                continue
            inj, stat = p.get("injury_status"), p.get("status")
            if inj or (stat and stat != "Active"):
                flagged[pid] = {"name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                                "pos": p.get("position"), "team": p.get("team"),
                                "injury_status": inj, "status": stat}
        write("injuries.json", {"fetched_at": STAMP, "players": dict(sorted(flagged.items()))})
    except RuntimeError as e:
        problems.append(str(e))

write("state.json", {"fetched_at": STAMP, "season": state.get("season"), "season_type": state.get("season_type"),
                     "week": state.get("week"), "leg": state.get("leg"), "display_week": state.get("display_week"),
                     "leagues": leagues_written, "problems": problems})
print(f"week {week}: leagues={leagues_written} claims={sum(len(v['claims']) for v in bids['leagues'].values())} problems={problems}")
if len(leagues_written) < len(CFG["leagues"]):
    sys.exit(1)
