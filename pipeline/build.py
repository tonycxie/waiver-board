"""Blend waiver sources, FAAB estimates, Sleeper league data and FantasyPros/DynastyProcess
rankings into dashboard data for Tony's superflex leagues.

Inputs (all optional except player DB; missing pieces are reported, not fatal):
  sources/*.json       consensus pickup lists (one per outlet)
  faab/*.json          FAAB bid estimates (RotoBaller, Fantasy Squawk, Dynasty Nerds, Faablab)
  data/sleeper_*.json  league rosters (from the Sleeper API)
  data/sleeper_bids.json  completed waiver claims with winning bids
  DynastyProcess repo: db_playerids.csv, db_fpecr_latest.csv, values-players.csv
Output:
  out/dashboard.json
"""
import csv, glob, json, math, os, re, statistics as st, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DP = os.environ.get("DP_DIR", "/home/claude/dynastyprocess/data/files")
TARGET_WEEK = int(os.environ.get("TARGET_WEEK", "7"))   # the NFL week the pickups would play
SOURCE_WEEK = int(os.environ.get("SOURCE_WEEK", "6"))   # week label of the waiver columns used
OFFENSE = {"QB", "RB", "WR", "TE"}
MY_OWNER = "471931546073296896"
# In-season cutoff: Sleeper files offseason claims under week 1. 2026-09-01 00:00 UTC.
SEASON_START_MS = int(os.environ.get("SEASON_START_MS", "1788220800000"))

SOURCE_ORDER = ["fantasypros", "espn", "yahoo", "cbs", "nfl", "footballers", "rotoballer",
                "sharp", "footballguys", "dynastynerds", "ftn", "rotowire"]
LEAGUE_FILES = [("seacucks", "sleeper_seacucks.json"), ("bestie", "sleeper_bestie.json"),
                ("champions", "sleeper_champions.json")]


def norm(name):
    n = name.lower().replace("’", "'")
    n = re.sub(r"[.'`]", "", n)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def num(x):
    try:
        if x in (None, "", "NA"):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- player database
players_db = list(csv.DictReader(open(os.path.join(DP, "db_playerids.csv"))))
by_sleeper = {r["sleeper_id"]: r for r in players_db if r["sleeper_id"] not in ("", "NA")}
by_name = {}
for r in players_db:
    if r["position"] in OFFENSE and r["sleeper_id"] not in ("", "NA"):
        by_name.setdefault(norm(r["name"]), []).append(r)
TEAM_FIX = {"JAC": "JAC", "JAX": "JAC", "GB": "GBP", "GBP": "GBP", "KC": "KCC", "KCC": "KCC",
            "LV": "LVR", "LVR": "LVR", "NE": "NEP", "NEP": "NEP", "NO": "NOS", "NOS": "NOS",
            "SF": "SFO", "SFO": "SFO", "TB": "TBB", "TBB": "TBB", "WAS": "WAS", "WSH": "WAS",
            "LA": "LAR", "LAR": "LAR"}
DISPLAY_TEAM = {"JAC": "JAX", "GBP": "GB", "KCC": "KC", "LVR": "LV", "NEP": "NE", "NOS": "NO", "SFO": "SF", "TBB": "TB"}


def lookup(name, pos=None, team=None):
    cands = by_name.get(norm(name), [])
    if not cands:
        # last-name + team fallback (handles nicknames like "Hollywood" Brown)
        parts = norm(name).split()
        if parts and team:
            t = TEAM_FIX.get(team.upper(), team.upper())
            cands = [r for k, rs in by_name.items() if k.split()[-1:] == parts[-1:] for r in rs
                     if r["team"] == t and (not pos or r["position"] == pos)]
    if len(cands) > 1 and pos:
        c2 = [r for r in cands if r["position"] == pos]
        cands = c2 or cands
    if len(cands) > 1 and team:
        t = TEAM_FIX.get(team.upper(), team.upper())
        c2 = [r for r in cands if r["team"] == t]
        cands = c2 or cands
    if len(cands) > 1:  # prefer most recent db season entry
        cands = sorted(cands, key=lambda r: r.get("db_season", ""), reverse=True)
    return cands[0] if cands else None


# ---------------------------------------------------------------- FantasyPros ranks & values
ecr = list(csv.DictReader(open(os.path.join(DP, "db_fpecr_latest.csv"))))
fp_by_id = {}
for r in ecr:
    fp_by_id.setdefault(r["id"], {})[r["page_type"]] = r
ecr_date = ecr[0]["scrape_date"] if ecr else None
values = {r["fp_id"]: r for r in csv.DictReader(open(os.path.join(DP, "values-players.csv")))}
values_date = next(iter(values.values()))["scrape_date"] if values else None


def fp_rows(p):
    return fp_by_id.get(p.get("fantasypros_id", ""), {})


def ros_pos_rank(p):
    r = fp_rows(p).get("redraft-" + p["position"].lower())
    return num(r["ecr"]) if r else None


def ros_overall(p):
    r = fp_rows(p).get("redraft-overall")
    return num(r["ecr"]) if r else None


# Weekly ranks: prefer data/fp_weekly.json (read live from FantasyPros in Chrome) when it is for
# TARGET_WEEK; otherwise fall back to the DynastyProcess mirror, but only if that scrape is for
# TARGET_WEEK too. A stale weekly scrape (e.g. last week's, where that week's bye teams are
# missing) must not flag healthy players as "not ranked this week".
def _week_of(datestr):
    from datetime import date
    y, m, d = (int(x) for x in datestr[:10].split("-"))
    return (date(y, m, d) - date(2026, 9, 9)).days // 7 + 1

FP_WEEKLY = {}
_fw = os.path.join(ROOT, "data", "fp_weekly.json")
if os.path.exists(_fw):
    _d = json.load(open(_fw))
    if int(_d.get("week", -1)) == TARGET_WEEK:
        FP_WEEKLY = {str(k): v for k, v in _d.get("players", {}).items()}
_wk_dates = {r["scrape_date"] for r in ecr if r["page_type"].startswith("weekly")}
MIRROR_WEEKLY_WEEK = max((_week_of(x) for x in _wk_dates), default=None)
if FP_WEEKLY:
    WEEKLY_SOURCE = f"FantasyPros live, week {TARGET_WEEK}"
elif MIRROR_WEEKLY_WEEK == TARGET_WEEK:
    WEEKLY_SOURCE = f"DynastyProcess mirror, week {TARGET_WEEK}"
else:
    WEEKLY_SOURCE = None  # stale: skip the "not ranked this week" check


def weekly_pos_rank(p):
    if FP_WEEKLY:
        r = FP_WEEKLY.get(str(p.get("fantasypros_id", "")))
        return num(r.get("pos_rank")) if r else None
    if WEEKLY_SOURCE is None:
        return "stale"
    r = fp_rows(p).get("weekly-" + p["position"].lower())
    return num(r["ecr"]) if r else None


def bye_week(p):
    lw = FP_WEEKLY.get(str(p.get("fantasypros_id", ""))) if FP_WEEKLY else None
    if lw and num(lw.get("bye")):
        return int(num(lw["bye"]))
    for r in fp_rows(p).values():
        b = num(r.get("bye"))
        if b:
            return int(b)
    return None


def owned(p):
    for key in ("weekly-op", "redraft-overall", "dynasty-op"):
        r = fp_rows(p).get(key)
        if r and num(r.get("player_owned_avg")) is not None:
            return num(r["player_owned_avg"])
    return None


def dynasty(p):
    v = values.get(p.get("fantasypros_id", ""))
    dsf = fp_rows(p).get("dynasty-op")
    return {"value_sf": int(num(v["value_2qb"])) if v else None,
            "ecr_sf": num(dsf["ecr"]) if dsf else None}


# ---------------------------------------------------------------- consensus pickups
source_meta, mentions = [], {}
for key in SOURCE_ORDER:
    path = os.path.join(ROOT, "sources", f"{key}.json")
    if not os.path.exists(path):
        source_meta.append({"key": key, "source": key, "status": "missing", "count": 0})
        continue
    d = json.load(open(path))
    plist = [x for x in d.get("players", []) if (x.get("pos") or "").upper() in OFFENSE]
    meta = {"key": key, "source": d.get("source", key), "url": d.get("url"), "week": d.get("week"),
            "status": d.get("status"), "ranking_type": d.get("ranking_type"), "count": len(plist),
            "unmatched": []}
    n = max(len(plist), 1)
    # Lists with no real priority order (unnumbered top-5s, alphabetical-by-position) get flat credit
    flat = d.get("ranking_type") == "unranked" or key == "dynastynerds"
    meta["flat_credit"] = flat
    # Bids in pickup columns are % of total budget unless the file says otherwise
    rem = d.get("budget_basis") == "remaining"
    meta["basis"] = "remaining" if rem else "total"
    for i, x in enumerate(sorted(plist, key=lambda x: x.get("rank") or 999)):
        p = lookup(x["name"], (x.get("pos") or "").upper(), x.get("team"))
        if not p:
            meta["unmatched"].append(x["name"])
            continue
        sid = p["sleeper_id"]
        m = mentions.setdefault(sid, {"p": p, "hits": [], "faab": [], "sf_faab": []})
        if any(h["key"] == key for h in m["hits"]):
            continue
        # Borda-style credit: 1.0 for the top pick, sliding to 0.3 for the last one listed
        credit = 0.65 if flat else (1.0 - 0.7 * (i / (n - 1)) if n > 1 else 1.0)
        m["hits"].append({"key": key, "source": meta["source"], "pos_in_list": i + 1, "of": len(plist),
                          "credit": round(credit, 3)})
        lo, hi = num(x.get("faab_pct_low")), num(x.get("faab_pct_high"))
        if lo is not None or hi is not None:
            m["faab"].append({"src": meta["source"], "lo": lo if lo is not None else hi, "hi": hi if hi is not None else lo, "rem": rem})
        slo, shi = num(x.get("sf_faab_pct_low")), num(x.get("sf_faab_pct_high"))
        if slo is not None or shi is not None:
            m["sf_faab"].append({"src": meta["source"], "lo": slo if slo is not None else shi, "hi": shi if shi is not None else slo, "rem": rem})
    source_meta.append(meta)

# ---------------------------------------------------------------- FAAB-specific sources
faab_meta = []
for path in sorted(glob.glob(os.path.join(ROOT, "faab", "*.json"))):
    d = json.load(open(path))
    meta = {"source": d.get("source"), "url": d.get("url"), "status": d.get("status"),
            "basis": d.get("budget_basis"), "count": len(d.get("players", [])), "unmatched": []}
    faab_meta.append(meta)
    rem = d.get("budget_basis") == "remaining"
    for x in d.get("players", []):
        pos = (x.get("pos") or "").upper()
        if pos not in OFFENSE:
            continue
        p = lookup(x["name"], pos, x.get("team"))
        if not p:
            meta["unmatched"].append(x["name"])
            continue
        m = mentions.setdefault(p["sleeper_id"], {"p": p, "hits": [], "faab": [], "sf_faab": []})
        lo, hi = num(x.get("faab_pct_low")), num(x.get("faab_pct_high"))
        if lo is not None or hi is not None:
            m["faab"] = [f for f in m["faab"] if f["src"] != d.get("source")]
            m["faab"].append({"src": d.get("source"), "lo": lo if lo is not None else hi, "hi": hi if hi is not None else lo, "rem": rem})
        slo, shi = num(x.get("sf_faab_pct_low")), num(x.get("sf_faab_pct_high"))
        if slo is not None or shi is not None:
            m["sf_faab"] = [f for f in m["sf_faab"] if f["src"] != d.get("source")]
            m["sf_faab"].append({"src": d.get("source"), "lo": slo if slo is not None else shi, "hi": shi if shi is not None else slo, "rem": rem})

QB_SF_SOURCES = {"RotoBaller", "Dynasty Nerds", "Fantasy Squawk"}


def faab_pool(pos, m):
    """The bids a player's estimate is built from. QB: every superflex-specific bid (RotoBaller, Dynasty
    Nerds, Fantasy Squawk, plus CBS/RotoWire/others when they print one). Others: Faablab if present,
    else every published FAAB range."""
    if pos == "QB":
        return m["sf_faab"], "superflex"
    fl = [f for f in m["faab"] if "faablab" in (f["src"] or "").lower()]
    return (fl, "faablab") if fl else (m["faab"], "published")


def r1h(x):
    # one decimal, halves up; logic.js's leagueBid uses the same arithmetic so the two always agree
    return math.floor(x * 10 + 0.5) / 10


def estimate(pool, basis, rem_frac=None, cap_pct=None):
    """Median bid range, as % of total budget.

    Some outlets (RotoBaller's FAAB guide, Fantasy Squawk) quote % of the budget you have LEFT. With
    rem_frac (this league's remaining / total) those are converted to % of total before the median,
    and the result is capped at what you have left. Without it (the league-agnostic number), the
    %-of-total bids are used when there are any; otherwise the %-of-remaining bids are shown as such."""
    if not pool:
        return None
    of_remaining = False
    if rem_frac is None:
        tot = [f for f in pool if not f.get("rem")]
        if tot:
            use = [(f["lo"], f["hi"]) for f in tot]
        else:
            use, of_remaining = [(f["lo"], f["hi"]) for f in pool], True
        srcs = tot or pool
    else:
        k = lambda f, v: v * rem_frac if f.get("rem") else v
        use = [(k(f, f["lo"]), k(f, f["hi"])) for f in pool]
        srcs = pool
    lo = st.median(u[0] for u in use)
    hi = st.median(u[1] for u in use)
    capped = False
    if cap_pct is not None and hi > cap_pct:
        lo, hi, capped = min(lo, cap_pct), cap_pct, True
    return {"lo": r1h(lo), "hi": r1h(hi), "mid": r1h((lo + hi) / 2), "n": len(srcs),
            "basis": basis, "of_remaining": of_remaining, "capped": capped,
            "from": sorted({f["src"] for f in srcs})}


def faab_estimate(pos, m, rem_frac=None, cap_pct=None):
    pool, basis = faab_pool(pos, m)
    return estimate(pool, basis, rem_frac, cap_pct)


# ---------------------------------------------------------------- leagues
leagues = []
for key, fn in LEAGUE_FILES:
    path = os.path.join(ROOT, "data", fn)
    if os.path.exists(path):
        L = json.load(open(path))
        L["key"] = key
        leagues.append(L)

bids = {}
bp = os.path.join(ROOT, "data", "sleeper_bids.json")
if os.path.exists(bp):
    bids = json.load(open(bp)).get("leagues", {})


def flex_share(L):
    rp = L["roster_positions"]
    c = {k: rp.count(k) for k in ("QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX")}
    need = {"QB": c["QB"] + 0.9 * c["SUPER_FLEX"],
            "RB": c["RB"] + 0.45 * c["FLEX"] + 0.04 * c["SUPER_FLEX"],
            "WR": c["WR"] + 0.45 * c["FLEX"] + 0.04 * c["SUPER_FLEX"],
            "TE": c["TE"] + 0.10 * c["FLEX"] + 0.02 * c["SUPER_FLEX"]}
    slots = {"QB": c["QB"] + c["SUPER_FLEX"], "RB": c["RB"], "WR": c["WR"], "TE": c["TE"]}
    return need, slots, c["FLEX"]


def roster_needs(L):
    need, slots, nflex = flex_share(L)
    mine = L["rosters"][str(L["my_roster_id"])]["players"]
    out = []
    flex_surplus = 0
    for pos in ("QB", "RB", "WR", "TE"):
        thresh = need[pos] * L["teams"]
        group = []
        for sid in mine:
            p = by_sleeper.get(sid)
            if not p or p["position"] != pos:
                continue
            r = ros_pos_rank(p)
            wk = weekly_pos_rank(p)
            bye = bye_week(p)
            group.append({"name": p["name"], "ros": r, "weekly": wk, "bye": bye})
        startable = [g for g in group if g["ros"] is not None and g["ros"] <= thresh]
        on_bye = [g["name"] for g in startable if g["bye"] == TARGET_WEEK]
        # in ROS startable range but missing from this week's rankings and not on a bye this week
        maybe_out = [g["name"] for g in startable if g["weekly"] is None and g["bye"] != TARGET_WEEK]
        avail = len(startable) - len(on_bye) - len(maybe_out)
        req = slots[pos]
        if avail < req:
            level = "need"
        elif avail == req or (pos != "QB" and avail < req + 1):
            level = "thin"
        else:
            level = "ok"
        flex_surplus += max(0, avail - req) if pos != "QB" else 0
        out.append({"pos": pos, "level": level, "startable": len(startable), "available_next_week": avail,
                    "slots": req, "on_bye": on_bye, "maybe_out": maybe_out,
                    "threshold": round(thresh)})
    if flex_surplus < nflex:
        out.append({"pos": "FLEX", "level": "need" if flex_surplus < nflex - 1 else "thin",
                    "startable": flex_surplus, "available_next_week": flex_surplus, "slots": nflex,
                    "on_bye": [], "maybe_out": [], "threshold": None})
    return out


def bid_history(L):
    out = {}
    for lid, season_label in ((L["league_id"], "this_season"), (L.get("previous_league_id"), "last_season")):
        if not lid or lid not in bids:
            continue
        if season_label == "last_season" and L["treat_as"] != "dynasty":
            continue
        b = bids[lid]
        budget = b.get("budget") or L["budget"]
        claims = []
        for c in b.get("claims", []):
            if not c.get("bid") or (c.get("created") or 0) < SEASON_START_MS:
                continue  # skip $0 claims and offseason/startup claims stored under week 1
            p = by_sleeper.get(str(c["player_id"]))
            if not p or p["position"] not in OFFENSE:
                continue
            claims.append({"player": p["name"] if p else str(c["player_id"]),
                           "pos": p["position"] if p else "?", "pct": round(100 * c["bid"] / budget, 1),
                           "week": c.get("week"), "mine": str(c.get("roster_id")) == str(L["my_roster_id"]) and season_label == "this_season"})
        by_pos = {}
        for pos in ("QB", "RB", "WR", "TE"):
            v = sorted(c["pct"] for c in claims if c["pos"] == pos)
            if v:
                by_pos[pos] = {"n": len(v), "median": round(st.median(v), 1), "max": max(v),
                               "p75": round(v[int(0.75 * (len(v) - 1))], 1)}
        out[season_label] = {"season": b.get("season"), "n_paid_claims": len(claims), "by_pos": by_pos,
                             "top": sorted(claims, key=lambda c: -c["pct"])[:8]}
    return out


def faab_position(L):
    budget = L["budget"]
    rem = {rid: max(0, budget - max(0, r["budget_used"])) for rid, r in L["rosters"].items()}
    mine = rem[str(L["my_roster_id"])]
    others = [v for k, v in rem.items() if k != str(L["my_roster_id"])]
    return {"budget": budget, "my_remaining": mine, "my_remaining_pct": round(100 * mine / budget, 1),
            "max_opp_remaining_pct": round(100 * max(others) / budget, 1),
            "teams_with_more": sum(1 for v in others if v > mine),
            "median_opp_remaining_pct": round(100 * st.median(others) / budget, 1)}


league_out = []
rostered = {}
for L in leagues:
    owned_ids = {p for r in L["rosters"].values() for p in r["players"]}
    mine = set(L["rosters"][str(L["my_roster_id"])]["players"])
    rostered[L["key"]] = (owned_ids, mine)
    league_out.append({"key": L["key"], "name": L["name"], "platform": "Sleeper", "treat_as": L["treat_as"],
                       "teams": L["teams"], "faab": faab_position(L), "needs": roster_needs(L),
                       "bid_history": bid_history(L), "league_id": L["league_id"],
                       "fetched_at": L.get("fetched_at"), "stale": False})
# Yahoo leagues: written during a MacBook run (read through Claude in Chrome), one file per league:
# data/yahoo_<key>.json = {"name": "...", "budget": 100, "teams": 12, "my_remaining": 37,
#   "max_opp_remaining": 88, "my_roster": ["Player Name", ...],
#   "status": {"Player Name": "available" | "taken" | "mine", ...},   # for every candidate checked
#   "claims": [{"player": "Name", "pos": "RB", "bid": 12, "week": 5}],  # optional bid history
#   "as_of_week": 9, "fetched_at": "2026-11-04T19:00-08:00"}           # when it was read
# Cloud-only runs keep the last file, so it is stale whenever as_of_week isn't this run's SOURCE_WEEK.
yahoo = []
for path in sorted(glob.glob(os.path.join(ROOT, "data", "yahoo_*.json"))):
    Y = json.load(open(path))
    Y["key"] = os.path.basename(path)[6:-5]
    Y["status_norm"] = {norm(k): v for k, v in Y.get("status", {}).items()}
    yahoo.append(Y)
    claims = [{"player": c["player"], "pos": c.get("pos", "?"), "pct": round(100 * c["bid"] / Y["budget"], 1),
               "week": c.get("week"), "mine": bool(c.get("mine"))} for c in Y.get("claims", []) if c.get("bid")]
    by_pos = {}
    for pos in ("QB", "RB", "WR", "TE"):
        v = sorted(c["pct"] for c in claims if c["pos"] == pos)
        if v:
            by_pos[pos] = {"n": len(v), "median": round(st.median(v), 1), "max": max(v),
                           "p75": round(v[int(0.75 * (len(v) - 1))], 1)}
    league_out.append({"key": Y["key"], "name": Y["name"], "platform": "Yahoo", "treat_as": "redraft",
                       "teams": Y.get("teams"),
                       "faab": {"budget": Y["budget"], "my_remaining": Y.get("my_remaining"),
                                "my_remaining_pct": round(100 * Y["my_remaining"] / Y["budget"], 1) if Y.get("my_remaining") is not None else None,
                                "max_opp_remaining_pct": round(100 * Y["max_opp_remaining"] / Y["budget"], 1) if Y.get("max_opp_remaining") is not None else None,
                                "teams_with_more": sum(1 for v in Y.get("opp_remaining", []) if v > (Y.get("my_remaining") or 0)) if Y.get("opp_remaining") else None,
                                "median_opp_remaining_pct": round(100 * st.median(Y["opp_remaining"]) / Y["budget"], 1) if Y.get("opp_remaining") else None},
                       "bid_history": {"this_season": {"n_paid_claims": len(claims), "by_pos": by_pos,
                                                       "top": sorted(claims, key=lambda c: -c["pct"])[:8]}} if claims else {},
                       "needs": [], "as_of_week": Y.get("as_of_week"), "fetched_at": Y.get("fetched_at"),
                       "stale": Y.get("as_of_week") != SOURCE_WEEK})
if not yahoo:
    league_out.append({"key": "yahoo", "name": "Yahoo leagues (2)", "platform": "Yahoo", "treat_as": "redraft",
                       "pending": "Read through Claude in Chrome on a MacBook run"})

# ---------------------------------------------------------------- assemble players
rows = []
for sid, m in mentions.items():
    p = m["p"]
    pos = p["position"]
    score = round(sum(h["credit"] for h in m["hits"]), 3)
    avail = {}
    for L in leagues:
        owned_ids, mine = rostered[L["key"]]
        avail[L["key"]] = "mine" if sid in mine else ("taken" if sid in owned_ids else "available")
    for Y in yahoo:
        avail[Y["key"]] = Y["status_norm"].get(norm(p["name"]), "unknown")
    pool, pool_basis = faab_pool(pos, m)
    league_faab = {}
    for LO in league_out:
        fa = LO.get("faab") or {}
        if fa.get("my_remaining") is None or not fa.get("budget"):
            continue
        league_faab[LO["key"]] = estimate(pool, pool_basis, fa["my_remaining"] / fa["budget"], fa["my_remaining_pct"])
    rows.append({"sleeper_id": sid, "name": p["name"], "pos": pos,
                 "team": DISPLAY_TEAM.get(p["team"], p["team"]), "age": num(p.get("age")),
                 "mentions": len(m["hits"]), "score": score,
                 "sources": [h["source"] for h in sorted(m["hits"], key=lambda h: h["pos_in_list"])],
                 "faab": estimate(pool, pool_basis), "league_faab": league_faab,
                 "faab_pool": pool, "faab_pool_basis": pool_basis,
                 "faab_inputs": m["faab"], "sf_faab_inputs": m["sf_faab"],
                 "availability": avail, "ros_pos_rank": ros_pos_rank(p), "ros_overall": ros_overall(p),
                 "owned_pct": owned(p), "bye": bye_week(p), "dynasty": dynasty(p)})

rows = [r for r in rows if r["mentions"] > 0 or (r["faab"] and r["faab"]["mid"] >= 1)]
rows.sort(key=lambda r: (-r["score"], -(r["faab"]["mid"] if r["faab"] else 0)))
for i, r in enumerate(rows, 1):
    r["consensus_rank"] = i

ok_sources = [s for s in source_meta if s.get("status") in ("ok", "partial") and s["count"]]
out = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    "source_week": SOURCE_WEEK, "target_week": TARGET_WEEK,
    "sources": source_meta, "faab_sources": faab_meta, "n_sources_used": len(ok_sources),
    "reference": {"fantasypros_ecr_date": ecr_date, "dynasty_values_date": values_date,
                  "dynasty_values": "DynastyProcess superflex values (FantasyPros dynasty ECR)"},
    "leagues": league_out, "players": rows,
}
os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
json.dump(out, open(os.path.join(ROOT, "out", "dashboard.json"), "w"), indent=1)

# ---------------------------------------------------------------- flat datasets for the Dashboard artifact
SHORT = {"seacucks": "Seacucks", "bestie": "Bestie Balls", "champions": "Champions"}
for Y in yahoo:
    SHORT[Y["key"]] = Y["name"]
league_keys = [L["key"] for L in league_out if "pending" not in L]
dyn_keys = [L["key"] for L in league_out if L.get("treat_as") == "dynasty"]


def fmt_range(f):
    if not f:
        return None
    t = f"{f['lo']:g}%" if f["lo"] == f["hi"] else f"{f['lo']:g}–{f['hi']:g}%"
    return t + " of left" if f.get("of_remaining") else t


BASIS_LABEL = {"superflex": "Superflex QB bid", "faablab": "Faablab", "published": "Published FAAB"}


def fmt_basis(f, league=False):
    if not f:
        return None
    t = BASIS_LABEL[f["basis"]] + f" ({', '.join(f['from'])})"
    if f.get("of_remaining"):
        t += ", % of the budget you have left"
    elif league:
        t += ", % of total; %-of-remaining bids scaled to your FAAB left" + (", capped at what you have" if f.get("capped") else "")
    return t


ds_pickups = []
for r in rows:
    f = r["faab"]
    row = {"rank": r["consensus_rank"], "player": r["name"], "pos": r["pos"], "team": r["team"],
           "mentions": r["mentions"], "score": r["score"], "sources": ", ".join(r["sources"]),
           "faab": fmt_range(f), "faab_mid": f["mid"] if f else None,
           "faab_basis": fmt_basis(f),
           "ros_pos_rank": round(r["ros_pos_rank"]) if r["ros_pos_rank"] else None,
           "owned_pct": r["owned_pct"], "bye": r["bye"],
           "dyn_value": r["dynasty"]["value_sf"], "dyn_rank": round(r["dynasty"]["ecr_sf"]) if r["dynasty"]["ecr_sf"] else None}
    AV = {"available": "Open", "taken": "Taken", "mine": "Yours", "unknown": "Unchecked"}
    for k in league_keys:
        row["avail_" + k] = AV[r["availability"].get(k, "unknown")]
        lf = r["league_faab"].get(k)
        row["bid_" + k] = fmt_range(lf)
        row["bidmid_" + k] = lf["mid"] if lf else None
        row["bidbasis_" + k] = fmt_basis(lf, league=True)
    row["open_in"] = sum(1 for k in league_keys if r["availability"].get(k) == "available")
    row["open_leagues"] = ", ".join(SHORT.get(k, k) for k in league_keys if r["availability"].get(k) == "available")
    ds_pickups.append(row)

ds_leagues, ds_needs, ds_bidpos, ds_topbids = [], [], [], []
for L in league_out:
    fa = L.get("faab") or {}
    ds_leagues.append({"league": L["key"], "name": SHORT.get(L["key"], L["name"]), "full_name": L["name"],
                       "platform": L["platform"], "type": L["treat_as"].title(), "teams": L.get("teams"),
                       "budget": fa.get("budget"), "my_remaining_pct": fa.get("my_remaining_pct"),
                       "max_opp_remaining_pct": fa.get("max_opp_remaining_pct"),
                       "teams_with_more": fa.get("teams_with_more"),
                       "median_opp_remaining_pct": fa.get("median_opp_remaining_pct"),
                       "status": L.get("pending") or "Loaded",
                       "stale": bool(L.get("stale")),
                       "as_of": (f"Week {L['as_of_week']} data" if L.get("as_of_week") else "last MacBook run") if L.get("stale") else None,
                       "fetched_at": L.get("fetched_at"),
                       "flags": sum(1 for n in L.get("needs", []) if n["level"] != "ok")})
    for n in L.get("needs", []):
        detail = []
        if n["on_bye"]:
            detail.append(f"Week {TARGET_WEEK} bye: " + ", ".join(n["on_bye"]))
        if n["maybe_out"]:
            detail.append("Not ranked this week: " + ", ".join(n["maybe_out"]))
        ds_needs.append({"id": f"{L['key']}-{n['pos']}", "league": L["key"], "name": SHORT.get(L["key"], L["name"]),
                         "pos": n["pos"], "level": {"need": "Need", "thin": "Thin", "ok": "OK"}[n["level"]],
                         "ready": n["available_next_week"],
                         "slots": n["slots"], "detail": "; ".join(detail) or None})
    for season, h in (L.get("bid_history") or {}).items():
        for pos, s in h["by_pos"].items():
            ds_bidpos.append({"id": f"{L['key']}-{season}-{pos}", "league": L["key"],
                              "name": SHORT.get(L["key"], L["name"]),
                              "season": h.get("season") or season, "season_label": season.replace("_", " "),
                              "pos": pos, "claims": s["n"], "median_pct": s["median"], "p75_pct": s["p75"],
                              "max_pct": s["max"]})
        for i, c in enumerate(h["top"]):
            ds_topbids.append({"id": f"{L['key']}-{season}-{i}", "league": L["key"],
                               "name": SHORT.get(L["key"], L["name"]), "season_label": season.replace("_", " "),
                               "player": c["player"], "pos": c["pos"], "pct": c["pct"], "week": c["week"],
                               "mine": "Yes" if c["mine"] else ""})

ST = {"ok": "Loaded", "partial": "Partly loaded", "paywalled": "Paywalled", "unreachable": "Couldn't load",
      "missing": "Not run", "gated": "Needs your own bids"}
ds_sources = []
for s in source_meta:
    ds_sources.append({"source": s["source"], "role": "Waiver pickups", "status": ST.get(s.get("status"), s.get("status")),
                       "players": s["count"], "week": s.get("week"), "url": s.get("url")})
for s in faab_meta:
    ds_sources.append({"source": s["source"], "role": "FAAB bids", "status": ST.get(s.get("status"), s.get("status")),
                       "players": s["count"], "week": SOURCE_WEEK, "url": s.get("url")})
ds_sources.append({"source": "Sleeper API", "role": "Rosters & winning bids", "status": "Loaded" if leagues else "Not run",
                   "players": None, "week": None, "url": "https://api.sleeper.app"})
if yahoo:
    yw = sorted({Y.get("as_of_week") for Y in yahoo}, key=lambda w: w or 0)
    stale = any(Y.get("as_of_week") != SOURCE_WEEK for Y in yahoo)
    ds_sources.append({"source": "Yahoo league pages", "role": "Rosters, FAAB & winning bids",
                       "status": (f"Stale (Week {yw[0]} data)" if yw[0] else "Stale (last MacBook run)") if stale else "Loaded",
                       "players": None, "week": yw[-1], "url": "https://football.fantasysports.yahoo.com"})
ds_sources.append({"source": "DynastyProcess / FantasyPros ECR", "role": "Ranks, byes, dynasty values",
                   "status": "Loaded", "players": None, "week": None, "url": "https://github.com/dynastyprocess/data"})

ds_run = [{"source_week": SOURCE_WEEK, "target_week": TARGET_WEEK,
           "generated": datetime.now(timezone.utc).astimezone().strftime("%a %b %-d, %-I:%M %p %Z"),
           "sources_used": len(ok_sources), "sources_total": len(SOURCE_ORDER),
           "ecr_date": ecr_date, "values_date": values_date,
           "dynasty_leagues": ", ".join(SHORT.get(k, k) for k in dyn_keys),
           "weekly_ranks": WEEKLY_SOURCE or f"stale (mirror is week {MIRROR_WEEKLY_WEEK}); injury check skipped",
           "league_keys": ",".join(league_keys)}]

for name, data in (("pickups", ds_pickups), ("leagues", ds_leagues), ("needs", ds_needs),
                   ("bid_by_pos", ds_bidpos), ("top_bids", ds_topbids), ("sources", ds_sources), ("run", ds_run)):
    json.dump(data, open(os.path.join(ROOT, "out", f"{name}.json"), "w"), indent=1)
print(f"weekly ranks: {WEEKLY_SOURCE or 'STALE mirror week ' + str(MIRROR_WEEKLY_WEEK)}")
print(f"players={len(rows)} sources_ok={len(ok_sources)}/12 leagues={len(leagues)} bids={'yes' if bids else 'no'}")
for s in source_meta:
    print(f"  {s['source']:<24} {s.get('status')!s:<11} n={s['count']:<3} unmatched={s.get('unmatched', [])[:5]}")
for s in faab_meta:
    print(f"  FAAB {s['source']!s:<19} {s.get('status')!s:<11} n={s['count']:<3} unmatched={s.get('unmatched', [])[:5]}")
