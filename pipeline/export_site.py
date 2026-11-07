"""Export this week's board for the public Sleeper site (github.com/tonycxie/waiver-board).

Run after build.py. Writes, under <site>/data/:
  board.json         week, run details, the ranking model, sources, the pickups (league-agnostic), and
                     past weeks' bids for per-league calibration
  players.json       every offensive player by Sleeper id: name, pos, team, ROS pos rank, weekly pos rank
                     (only when it is for TARGET_WEEK), bye, superflex and 1-QB dynasty values and ranks
  history/weekNN.json  this week's general bids and scores, kept for calibration and later review

Ranks: superflex rank = build.py's board score (value + heat). The 1-QB rank uses the same heat with 1-QB
value (QB starter depth 12 instead of 24). For QBs, the 1-QB bid is the median of outlets' standard bids.

Usage: SOURCE_WEEK=N TARGET_WEEK=N python3 -I export_site.py /home/claude/waiver-board
Nothing league-specific (rosters, owner ids, Yahoo) is written: the page pulls that live from Sleeper.
"""
import csv, glob, json, os, sys
from statistics import median
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DP = os.environ.get("DP_DIR", "/home/claude/dynastyprocess/data/files")
SITE = sys.argv[1] if len(sys.argv) > 1 else "/home/claude/waiver-board"
for _k in ("TARGET_WEEK", "SOURCE_WEEK"):
    if not os.environ.get(_k):
        sys.exit(f"{_k} is not set. Run as: SOURCE_WEEK=N TARGET_WEEK=N python3 -I export_site.py <site dir>")
TARGET_WEEK = int(os.environ["TARGET_WEEK"])
SOURCE_WEEK = int(os.environ["SOURCE_WEEK"])
# Set when re-exporting an earlier week, so its history file keeps the time that board was first published
GENERATED_AT = os.environ.get("GENERATED_AT")
OFFENSE = {"QB", "RB", "WR", "TE"}
DISPLAY_TEAM = {"JAC": "JAX", "GBP": "GB", "KCC": "KC", "LVR": "LV", "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB"}


def num(x):
    try:
        return None if x in (None, "", "NA") else float(x)
    except (TypeError, ValueError):
        return None


dash = json.load(open(os.path.join(ROOT, "out", "dashboard.json")))
wr = json.load(open(os.path.join(ROOT, "out", "weekly_ranks.json")))
ids = [r for r in csv.DictReader(open(os.path.join(DP, "db_playerids.csv")))
       if r["position"] in OFFENSE and r["sleeper_id"] not in ("", "NA")]
ecr = list(csv.DictReader(open(os.path.join(DP, "db_fpecr_latest.csv"))))
fp = {}
for r in ecr:
    fp.setdefault(r["id"], {})[r["page_type"]] = r
values = {r["fp_id"]: r for r in csv.DictReader(open(os.path.join(DP, "values-players.csv")))}
ecr_date = ecr[0]["scrape_date"] if ecr else None
values_date = next(iter(values.values()))["scrape_date"] if values else None


def value_ranks(field):
    v = sorted(((num(r.get(field)), k) for k, r in values.items() if num(r.get(field)) is not None), reverse=True)
    return {k: i for i, (_, k) in enumerate(v, 1)}


DYN_RANK, DYN_RANK_1QB = value_ranks("value_2qb"), value_ranks("value_1qb")

# Weekly ranks: whatever build.py used (out/weekly_ranks.json), so the site and Tony's board agree.
weekly_ok = wr.get("week") == TARGET_WEEK and bool(wr.get("source"))
live_ranks, live_byes = wr.get("ranks") or {}, wr.get("byes") or {}

players = {}
for p in sorted(ids, key=lambda r: r.get("db_season", ""), reverse=True):
    sid = p["sleeper_id"]
    if sid in players:
        continue
    fid = p["fantasypros_id"]
    rows = fp.get(fid, {})
    v = values.get(fid)
    ros = rows.get("redraft-" + p["position"].lower())
    if not rows and not v:
        continue
    wk = None
    if weekly_ok:
        if wr.get("mirror"):
            w = rows.get("weekly-" + p["position"].lower())
            wk = num(w["ecr"]) if w else None
        else:
            wk = num(live_ranks.get(str(fid)))
    bye = int(live_byes[str(fid)]) if live_byes.get(str(fid)) else None
    if bye is None:
        for r in rows.values():
            if num(r.get("bye")):
                bye = int(num(r["bye"]))
                break
    players[sid] = [p["name"], p["position"], DISPLAY_TEAM.get(p["team"], p["team"]),
                    # one decimal: the lineup check compares it to a fractional starter cutoff, as build.py does
                    round(num(ros["ecr"]), 1) if ros and num(ros["ecr"]) else None,
                    round(wk) if wk else None, bye,
                    int(num(v["value_2qb"])) if v and num(v["value_2qb"]) is not None else None,
                    int(num(v["value_1qb"])) if v and num(v["value_1qb"]) is not None else None,
                    DYN_RANK.get(fid), DYN_RANK_1QB.get(fid)]


def general(pool):
    """League-agnostic bid, same rule as build.py's estimate(): %-of-total bids when there are any,
    otherwise the %-of-remaining bids, flagged as such."""
    tot = [f for f in pool if not f.get("rem")]
    use = tot or pool
    return {"lo": median([f["lo"] for f in use]), "hi": median([f["hi"] for f in use]),
            "from": sorted({f["src"] for f in use}), "of_remaining": not tot}


def compact(pool):
    # [lo, hi, 1 if % of remaining else 0] per outlet; the page converts these per league
    return [[f["lo"], f["hi"], 1 if f.get("rem") else 0] for f in pool]


def rng(lo, hi, rem=False, approx=False):
    t = f"{lo:g}%" if lo == hi else f"{lo:g}–{hi:g}%"
    return ("~" if approx else "") + t + (" of left" if rem else "")


flat = {r["player"]: r for r in json.load(open(os.path.join(ROOT, "out", "pickups.json")))}
KEEP = ["rank", "player", "pos", "team", "mentions", "score", "value", "heat", "heat_label", "trend_rank", "sources",
        "faab", "faab_mid", "faab_basis", "ros", "ros_pos_rank", "owned_pct", "bye", "dyn_value", "dyn_rank"]
pickups = []
for r in dash["players"]:
    f = flat[r["name"]]
    row = {k: f.get(k) for k in KEEP}
    row["sid"] = r["sleeper_id"]
    row["deep"] = bool(r["deep"])
    row["value_1qb"], row["score_1qb"], row["rank_1qb"] = r["value_1qb"], r["score_1qb"], r["rank_1qb"]
    e = r["faab"]
    approx = bool(e and e.get("approx"))
    row["faab_in"] = [] if approx else compact(r.get("faab_pool") or [])
    row["faab_est"] = [e["lo"], e["hi"]] if approx else None
    if r["pos"] == "QB":
        # 1-QB leagues: the median of outlets' standard bids (never superflex bids)
        pool = [x for x in r.get("faab_inputs", []) if "faablab" not in (x.get("src") or "").lower()]
        q = general(pool) if pool else None
        row["faab_in_1qb"] = compact(pool)
        row["faab_est_1qb"] = None
        row["faab_1qb"] = rng(q["lo"], q["hi"], q["of_remaining"]) if q else None
        row["faab_1qb_mid"] = round((q["lo"] + q["hi"]) / 2, 1) if q else None
        row["faab_1qb_basis"] = (f"1-QB bid ({', '.join(q['from'])})" + (", % of the budget you have left" if q["of_remaining"] else "")) if q else None
    else:
        row["faab_1qb"], row["faab_1qb_mid"], row["faab_1qb_basis"] = row["faab"], row["faab_mid"], row["faab_basis"]
        row["faab_in_1qb"], row["faab_est_1qb"] = row["faab_in"], row["faab_est"]
    p1 = players.get(r["sleeper_id"])
    row["dyn_value_1qb"] = p1[7] if p1 else None
    pickups.append(row)

run = (json.load(open(os.path.join(ROOT, "out", "run.json"))) or [{}])[0]
sources = [{k: s.get(k) for k in ("source", "role", "status", "players", "url")}
           for s in json.load(open(os.path.join(ROOT, "out", "sources.json"))) if s.get("source") not in ("Sleeper API",)]
now = GENERATED_AT or datetime.now(timezone.utc).isoformat(timespec="minutes")

# ---------------------------------------------------------------- weekly history (calibration)
# General bids, % of total, published only (no estimates, no %-of-left): what a league's winning bids are
# compared against. Superflex bids for QBs; "bids_1qb" has QB standard bids for 1-QB leagues.
hist_dir = os.path.join(SITE, "data", "history")
os.makedirs(hist_dir, exist_ok=True)
hist = {"week": SOURCE_WEEK, "generated_at": now,
        "bids": {r["sleeper_id"]: r["faab"]["mid"] for r in dash["players"]
                 if r["faab"] and not r["faab"]["approx"] and not r["faab"]["of_remaining"]},
        "bids_1qb": {p["sid"]: p["faab_1qb_mid"] for p in pickups
                     if p["pos"] == "QB" and p["faab_1qb_mid"] is not None and "of left" not in (p["faab_1qb"] or "")},
        "rows": [[r["sleeper_id"], r["consensus_rank"], r["score"], r["value"], r["heat_n"]] for r in dash["players"]]}
json.dump(hist, open(os.path.join(hist_dir, f"week{SOURCE_WEEK:02d}.json"), "w"), separators=(",", ":"))
bid_history = []
for hp in sorted(glob.glob(os.path.join(hist_dir, "week*.json"))):
    h = json.load(open(hp))
    if int(h["week"]) >= SOURCE_WEEK:
        continue  # a board can't calibrate itself
    bid_history.append({"week": h["week"], "at": int(datetime.fromisoformat(h["generated_at"]).timestamp() * 1000),
                        "bids": h["bids"], "bids_1qb": h.get("bids_1qb", {})})

board = {
    "source_week": SOURCE_WEEK, "target_week": TARGET_WEEK,
    "generated": run.get("generated") or datetime.now(timezone.utc).strftime("%a %b %-d, %-I:%M %p UTC"),
    "generated_at": now,
    "sources_used": run.get("sources_used"), "sources_total": run.get("sources_total", 12),
    "ecr_date": ecr_date, "values_date": values_date,
    "weekly_ranks_week": TARGET_WEEK if weekly_ok else None,
    "weekly_ranks_from": wr.get("source") if weekly_ok else None,
    "season": 2026, "season_start_ms": 1788220800000,
    "model": dash["model"], "sf_ratio": dash.get("sf_ratio"), "trending": dash.get("trending"),
    "deep_cuts": sum(1 for p in pickups if p["deep"]),
    "sources": sources, "pickups": pickups, "bid_history": bid_history,
}
os.makedirs(os.path.join(SITE, "data"), exist_ok=True)
json.dump(board, open(os.path.join(SITE, "data", "board.json"), "w"), separators=(",", ":"))
json.dump({"week": TARGET_WEEK,
           "fields": ["name", "pos", "team", "ros_pos_rank", "weekly_pos_rank", "bye", "dyn_value", "dyn_value_1qb",
                      "dyn_rank", "dyn_rank_1qb"],
           "players": players}, open(os.path.join(SITE, "data", "players.json"), "w"), separators=(",", ":"))
qbs = [p for p in pickups if p["pos"] == "QB"]
print(f"pickups={len(pickups)} deep={board['deep_cuts']} players={len(players)} "
      f"qb_1qb_bids={sum(1 for p in qbs if p['faab_1qb'])}/{len(qbs)} est_bids={sum(1 for p in pickups if p['faab_est'])} "
      f"weekly={'from ' + wr['source'] if weekly_ok else 'skipped (latest week ' + str(wr.get('week')) + ')'} "
      f"history_weeks={len(bid_history)} unmatched=[]")
