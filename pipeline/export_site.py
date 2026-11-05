"""Export this week's board for the public Sleeper site (github.com/tonycxie/waiver-board).

Run after build.py. Writes two files that the site's page reads:
  <site>/data/board.json    week, run details, sources, and the consensus pickups (league-agnostic)
  <site>/data/players.json  every offensive player by Sleeper id: name, pos, team, ROS pos rank,
                            weekly pos rank (only when it is for TARGET_WEEK), bye, superflex and 1-QB dynasty values

Each pickup carries superflex and 1-QB versions of its bid and rank. For QBs, the 1-QB bid is the median
of outlets' standard bids (build.py's faab_inputs), and the 1-QB rank discounts QB consensus scores by the
median ratio of DynastyProcess 1-QB to superflex QB values. RB/WR/TE are the same in both.

Usage: SOURCE_WEEK=N TARGET_WEEK=N python3 -I export_site.py /home/claude/waiver-board
Nothing league-specific (rosters, owner ids, Yahoo) is written: the page pulls that live from Sleeper.
"""
import csv, json, os, re, sys
from statistics import median
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DP = os.environ.get("DP_DIR", "/home/claude/dynastyprocess/data/files")
SITE = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/waiver-board"
TARGET_WEEK = int(os.environ.get("TARGET_WEEK", "9"))
SOURCE_WEEK = int(os.environ.get("SOURCE_WEEK", str(TARGET_WEEK)))
OFFENSE = {"QB", "RB", "WR", "TE"}
DISPLAY_TEAM = {"GBP": "GB", "KCC": "KC", "LVR": "LV", "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB"}
TEAM_FIX = {"JAC": "JAX", "GB": "GBP", "KC": "KCC", "LV": "LVR", "NE": "NEP", "NO": "NOS", "SF": "SFO",
            "TB": "TBB", "WSH": "WAS", "LA": "LAR"}


def norm(name):
    n = name.lower().replace("’", "'")
    n = re.sub(r"[.'`]", "", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def num(x):
    try:
        return None if x in (None, "", "NA") else float(x)
    except (TypeError, ValueError):
        return None


def week_of(datestr):
    y, m, d = (int(x) for x in datestr[:10].split("-"))
    return (date(y, m, d) - date(2026, 9, 9)).days // 7 + 1


ids = [r for r in csv.DictReader(open(os.path.join(DP, "db_playerids.csv")))
       if r["position"] in OFFENSE and r["sleeper_id"] not in ("", "NA")]
ecr = list(csv.DictReader(open(os.path.join(DP, "db_fpecr_latest.csv"))))
fp = {}
for r in ecr:
    fp.setdefault(r["id"], {})[r["page_type"]] = r
values = {r["fp_id"]: r for r in csv.DictReader(open(os.path.join(DP, "values-players.csv")))}
ecr_date = ecr[0]["scrape_date"] if ecr else None
values_date = next(iter(values.values()))["scrape_date"] if values else None

# Weekly ranks: a full live FantasyPros read (data/fp_weekly.json with most players) wins; otherwise the
# DynastyProcess mirror, but only when it is for TARGET_WEEK. Otherwise weekly ranks are left out and
# the page skips its "not ranked this week" check rather than flag healthy players.
live = {}
fw = os.path.join(ROOT, "data", "fp_weekly.json")
if os.path.exists(fw):
    d = json.load(open(fw))
    if int(d.get("week", -1)) == TARGET_WEEK and len(d.get("players", {})) >= 300:
        live = {str(k): v for k, v in d["players"].items()}
mirror_week = max((week_of(r["scrape_date"]) for r in ecr if r["page_type"].startswith("weekly")), default=None)
weekly_from = "live" if live else ("mirror" if mirror_week == TARGET_WEEK else None)

players = {}
seen_rank = set()
for p in sorted(ids, key=lambda r: r.get("db_season", ""), reverse=True):
    sid = p["sleeper_id"]
    if sid in players:
        continue
    rows = fp.get(p["fantasypros_id"], {})
    v = values.get(p["fantasypros_id"])
    ros = rows.get("redraft-" + p["position"].lower())
    if not rows and not v:
        continue
    wk = None
    if weekly_from == "live":
        lw = live.get(str(p["fantasypros_id"]))
        wk = num(lw.get("pos_rank")) if lw else None
    elif weekly_from == "mirror":
        wr = rows.get("weekly-" + p["position"].lower())
        wk = num(wr["ecr"]) if wr else None
    bye = None
    lw = live.get(str(p["fantasypros_id"])) if live else None
    if lw and num(lw.get("bye")):
        bye = int(num(lw["bye"]))
    else:
        for r in rows.values():
            if num(r.get("bye")):
                bye = int(num(r["bye"]))
                break
    players[sid] = [p["name"], p["position"], DISPLAY_TEAM.get(p["team"], p["team"]),
                    round(num(ros["ecr"])) if ros and num(ros["ecr"]) else None,
                    round(wk) if wk else None, bye,
                    int(num(v["value_2qb"])) if v and num(v["value_2qb"]) is not None else None,
                    int(num(v["value_1qb"])) if v and num(v["value_1qb"]) is not None else None]

# ---------------------------------------------------------------- pickups (league-agnostic columns only)
by_name = {}
for p in ids:
    by_name.setdefault(norm(p["name"]), []).append(p)


def sid_for(name, pos, team):
    c = [r for r in by_name.get(norm(name), []) if r["position"] == pos] or by_name.get(norm(name), [])
    if len(c) > 1 and team:
        t = TEAM_FIX.get(team.upper(), team.upper())
        c = [r for r in c if r["team"] == t] or c
    c = sorted(c, key=lambda r: r.get("db_season", ""), reverse=True)
    return c[0]["sleeper_id"] if c else None


dash = os.path.join(ROOT, "out", "dashboard.json")
sid_by_name, qb1 = {}, {}
if os.path.exists(dash):
    for r in json.load(open(dash)).get("players", []):
        sid_by_name[r["name"]] = r["sleeper_id"]
        # build.py keeps each outlet's standard (1-QB) bids in faab_inputs, superflex bids in sf_faab_inputs
        pool = [f for f in r.get("faab_inputs", []) if "faablab" not in (f.get("src") or "").lower()]
        if r["pos"] == "QB" and pool:
            qb1[r["name"]] = {"lo": median([f["lo"] for f in pool]), "hi": median([f["hi"] for f in pool]),
                              "from": sorted({f["src"] for f in pool})}
# Manual 1-QB QB bids (used when out/dashboard.json is missing, e.g. a rebuilt week)
mq = os.path.join(ROOT, "out", "faab_1qb.json")
if os.path.exists(mq):
    for k, v in json.load(open(mq)).items():
        qb1.setdefault(k, v)

# 1-QB leagues value QBs far less. Discount each QB's consensus score by the typical ratio of
# DynastyProcess 1-QB to superflex values (median over QBs with a superflex value of 200+).
_qb_fp = {r["fantasypros_id"] for r in ids if r["position"] == "QB"}
_ratios = [num(v["value_1qb"]) / num(v["value_2qb"]) for k, v in values.items()
           if k in _qb_fp and num(v.get("value_2qb")) and num(v["value_2qb"]) >= 200 and num(v.get("value_1qb")) is not None]
QB_1QB_FACTOR = round(median(_ratios), 3) if _ratios else 0.3
KEEP = ["rank", "player", "pos", "team", "mentions", "score", "sources", "faab", "faab_mid", "faab_basis",
        "ros_pos_rank", "owned_pct", "bye", "dyn_value", "dyn_rank"]
pickups, unmatched = [], []
for r in json.load(open(os.path.join(ROOT, "out", "pickups.json"))):
    sid = sid_by_name.get(r["player"]) or sid_for(r["player"], r["pos"], r["team"])
    if not sid:
        unmatched.append(r["player"])
    row = {k: r.get(k) for k in KEEP}
    row["sid"] = sid
    if r["pos"] == "QB":
        q = qb1.get(r["player"])
        row["faab_1qb"] = (f"{q['lo']:g}%" if q["lo"] == q["hi"] else f"{q['lo']:g}–{q['hi']:g}%") if q else None
        row["faab_1qb_mid"] = round((q["lo"] + q["hi"]) / 2, 1) if q else None
        row["faab_1qb_basis"] = f"1-QB bid ({', '.join(q['from'])})" if q else None
        row["score_1qb"] = round((r.get("score") or 0) * QB_1QB_FACTOR, 3)
    else:
        row["faab_1qb"], row["faab_1qb_mid"], row["faab_1qb_basis"] = r.get("faab"), r.get("faab_mid"), r.get("faab_basis")
        row["score_1qb"] = r.get("score") or 0
    p1 = players.get(sid) if sid else None
    row["dyn_value_1qb"] = p1[7] if p1 else None
    pickups.append(row)
for i, row in enumerate(sorted(pickups, key=lambda x: (-x["score_1qb"], -(x["faab_1qb_mid"] or 0))), 1):
    row["rank_1qb"] = i

run = {}
rp = os.path.join(ROOT, "out", "run.json")
if os.path.exists(rp):
    run = json.load(open(rp))[0]
sources = []
sp = os.path.join(ROOT, "out", "sources.json")
if os.path.exists(sp):
    sources = [{k: s.get(k) for k in ("source", "role", "status", "players", "url")}
               for s in json.load(open(sp)) if s.get("source") not in ("Sleeper API",)]

board = {
    "source_week": SOURCE_WEEK, "target_week": TARGET_WEEK,
    "generated": run.get("generated") or datetime.now(timezone.utc).strftime("%a %b %-d, %-I:%M %p UTC"),
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    "sources_used": run.get("sources_used"), "sources_total": run.get("sources_total", 12),
    "ecr_date": ecr_date, "values_date": values_date,
    "weekly_ranks_week": TARGET_WEEK if weekly_from else None,
    "weekly_ranks_from": {"live": "FantasyPros live", "mirror": "DynastyProcess mirror"}.get(weekly_from),
    "season": 2026, "season_start_ms": 1788220800000, "qb_1qb_factor": QB_1QB_FACTOR,
    "sources": sources, "pickups": pickups,
}
os.makedirs(os.path.join(SITE, "data"), exist_ok=True)
json.dump(board, open(os.path.join(SITE, "data", "board.json"), "w"), separators=(",", ":"))
json.dump({"week": TARGET_WEEK, "fields": ["name", "pos", "team", "ros_pos_rank", "weekly_pos_rank", "bye", "dyn_value", "dyn_value_1qb"],
           "players": players}, open(os.path.join(SITE, "data", "players.json"), "w"), separators=(",", ":"))
print(f"pickups={len(pickups)} unmatched={unmatched} players={len(players)} qb_1qb_factor={QB_1QB_FACTOR} "
      f"qb_1qb_bids={sum(1 for p in pickups if p['pos'] == 'QB' and p['faab_1qb'])}/{sum(1 for p in pickups if p['pos'] == 'QB')} "
      f"weekly={'from ' + weekly_from if weekly_from else 'skipped (mirror week ' + str(mirror_week) + ')'}")
