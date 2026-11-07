"""Blend waiver sources, FAAB estimates, Sleeper league data and FantasyPros/DynastyProcess
rankings into dashboard data for Tony's superflex leagues.

Ranking model (Nov 6): every player gets two scores.
  value  How good he is for superflex: FantasyPros rest-of-season positional rank on a curve where a
         position's starter depth scores 50 (QB24, RB30, WR40, TE14 in a generic 12-team superflex).
         Dynasty leagues use DynastyProcess superflex value instead.
  heat   How hard the market is chasing him: outlet lists (credit over a fixed top 25, top 30 kept per
         list; position-by-position lists credited within position) plus Sleeper trending adds.
  Board score = 0.65 x value + 0.35 x heat (heat scaled to a whole number, hottest player = 100). Heat is in the
  order so that news the Friday ranks haven't caught (a new starter) still counts.
  In a league tab, "For you" adds half of how far a player beats your worst starter at his position,
  plus 10 for a Need and 5 for a Thin slot. MODEL below is exported to the friends' site so logic.js
  uses the same numbers.

Inputs (all optional except the player DB; missing pieces are reported, not fatal):
  sources/*.json            consensus pickup lists (one per outlet)
  faab/*.json               FAAB bid estimates (RotoBaller, Fantasy Squawk, Dynasty Nerds, Faablab)
  data/sleeper_*.json       league rosters (Sleeper sync)
  data/sleeper_bids.json    completed waiver claims with winning bids
  data/sleeper_injuries.json  Sleeper injury designations, used if under 48 h old
  data/trending_add.json    Sleeper trending adds (sync), used if under 48 h old (TRENDING=0 turns off)
  data/yahoo_*.json         Yahoo leagues (MacBook runs)
  data/fp_weekly.json       FantasyPros weekly ranks read in Chrome (optional)
  $HISTORY_DIR/week*.json   past boards' bids, for each league's bid calibration
  DynastyProcess repo: db_playerids.csv, db_fpecr_latest.csv, fp_latest_weekly.csv, values-players.csv
Output: out/dashboard.json and the flat datasets in out/
Usage:  SOURCE_WEEK=N TARGET_WEEK=N python3 -I build.py
"""
import csv, glob, json, math, os, re, statistics as st, sys
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DP = os.environ.get("DP_DIR", "/home/claude/dynastyprocess/data/files")
HISTORY_DIR = os.environ.get("HISTORY_DIR", "/home/claude/waiver-board/data/history")
PT = ZoneInfo("America/Los_Angeles")


def _week_env(k):
    v = os.environ.get(k)
    if not v:
        sys.exit(f"{k} is not set. Run as: SOURCE_WEEK=N TARGET_WEEK=N python3 -I build.py")
    return int(v)


TARGET_WEEK = _week_env("TARGET_WEEK")   # the NFL week the pickups would play
SOURCE_WEEK = _week_env("SOURCE_WEEK")   # week label of the waiver columns used
OFFENSE = {"QB", "RB", "WR", "TE"}
# In-season cutoff: Sleeper files offseason claims under week 1. 2026-09-01 00:00 UTC.
SEASON_START_MS = int(os.environ.get("SEASON_START_MS", "1788220800000"))
WEEK1_THURSDAY = date(2026, 9, 10)

SOURCE_ORDER = ["fantasypros", "espn", "yahoo", "cbs", "nfl", "footballers", "rotoballer",
                "sharp", "footballguys", "dynastynerds", "ftn", "rotowire"]
LEAGUE_FILES = [("seacucks", "sleeper_seacucks.json"), ("bestie", "sleeper_bestie.json"),
                ("champions", "sleeper_champions.json")]
# Yahoo files carry no lineup settings; both of Tony's Yahoo leagues are superflex Q/W/R/T.
YAHOO_LINEUP = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "SUPER_FLEX"]

# Shared with logic.js through board.json -> "model". Change here, not there.
MODEL = {
    "value_weight": 0.65, "heat_weight": 0.35,
    "half": {"QB": 24, "RB": 30, "WR": 40, "TE": 14},       # positional rank where value = 50 (superflex)
    "half_1qb": {"QB": 12, "RB": 30, "WR": 40, "TE": 14},   # same for 1-QB leagues
    "dyn_half": 120,         # dynasty: overall superflex value rank where value = 50
    "missing_value": 5,      # no rest-of-season rank (deeper than FantasyPros ranks)
    "gain_weight": 0.5, "need_bonus": {"need": 10, "thin": 5},
    "calib": {"min_claims": 3, "shrink": 6, "lo": 0.5, "hi": 2.5, "max_days": 8, "min_pred": 1},
}
CREDIT = {"window": 25, "cap": 30, "pos_window": 8, "flat": 0.65, "top": 1.0, "floor": 0.3}


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


def r1h(x):
    # one decimal, halves up; logic.js uses the same arithmetic so the two always agree
    return math.floor(x * 10 + 0.5) / 10


def curve(rank, half):
    """0-100 value: 100 * 0.5 ** (rank / half). logic.js curve() is the same."""
    if rank is None:
        return MODEL["missing_value"]
    return r1h(100 * 0.5 ** (rank / half))


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


def _value_ranks(field):
    v = sorted(((num(r.get(field)), k) for k, r in values.items() if num(r.get(field)) is not None), reverse=True)
    return {k: i for i, (_, k) in enumerate(v, 1)}


DYN_RANK = _value_ranks("value_2qb")       # overall superflex dynasty rank by DynastyProcess value
DYN_RANK_1QB = _value_ranks("value_1qb")


def fp_rows(p):
    return fp_by_id.get(p.get("fantasypros_id", ""), {})


def ros_pos_rank(p):
    r = fp_rows(p).get("redraft-" + p["position"].lower())
    return num(r["ecr"]) if r else None


def ros_overall(p):
    r = fp_rows(p).get("redraft-overall")
    return num(r["ecr"]) if r else None


def _week_of(datestr):
    y, m, d = (int(x) for x in datestr[:10].split("-"))
    return (date(y, m, d) - date(2026, 9, 9)).days // 7 + 1


def _kick_week(ts):
    d = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(PT).date()
    return (d - WEEK1_THURSDAY).days // 7 + 1


# Weekly ranks, best first:
#  1. data/fp_weekly.json, read live from FantasyPros in Chrome on a MacBook run
#  2. DynastyProcess fp_latest_weekly.csv: FantasyPros weekly ranks, re-scraped twice a day. Its week is
#     read from the game kickoff times in the file.
#  3. the weekly pages in db_fpecr_latest.csv (Friday scrape)
# Each is used only if it is for TARGET_WEEK; otherwise the "not ranked this week" check is skipped,
# since last week's ranks (missing that week's bye teams) would flag healthy players.
def load_weekly():
    fw = os.path.join(ROOT, "data", "fp_weekly.json")
    if os.path.exists(fw):
        d = json.load(open(fw))
        if int(d.get("week", -1)) == TARGET_WEEK:
            return {str(k): v for k, v in d.get("players", {}).items()}, f"FantasyPros live (Chrome), week {TARGET_WEEK}", TARGET_WEEK
    lw = os.path.join(DP, "fp_latest_weekly.csv")
    if os.path.exists(lw):
        rows = [r for r in csv.DictReader(open(lw)) if r.get("pos") in OFFENSE]
        weeks = sorted(_kick_week(r["player_game_kickoff_ts"]) for r in rows if num(r.get("player_game_kickoff_ts")))
        wk = weeks[len(weeks) // 2] if weeks else None
        if wk == TARGET_WEEK:
            out = {}
            for r in rows:
                m = re.search(r"\d+", r.get("pos_rank") or "")
                out[str(r["fantasypros_id"])] = {"pos_rank": int(m.group()) if m else None,
                                                 "bye": int(num(r["player_bye_week"])) if num(r.get("player_bye_week")) else None}
            return out, f"FantasyPros weekly via DynastyProcess ({rows[0]['scrape_date']}), week {wk}", wk
    mw = max((_week_of(r["scrape_date"]) for r in ecr if r["page_type"].startswith("weekly")), default=None)
    if mw == TARGET_WEEK:
        return None, f"DynastyProcess Friday scrape, week {mw}", mw
    return {}, None, mw


FP_WEEKLY, WEEKLY_SOURCE, WEEKLY_WEEK = load_weekly()
USE_MIRROR_WEEKLY = FP_WEEKLY is None
FP_WEEKLY = FP_WEEKLY or {}


def weekly_pos_rank(p):
    if FP_WEEKLY:
        r = FP_WEEKLY.get(str(p.get("fantasypros_id", "")))
        return num(r.get("pos_rank")) if r else None
    if not USE_MIRROR_WEEKLY:
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
    fid = p.get("fantasypros_id", "")
    v = values.get(fid)
    dsf = fp_rows(p).get("dynasty-op")
    return {"value_sf": int(num(v["value_2qb"])) if v and num(v.get("value_2qb")) is not None else None,
            "value_1qb": int(num(v["value_1qb"])) if v and num(v.get("value_1qb")) is not None else None,
            "ecr_sf": num(dsf["ecr"]) if dsf else None,
            "rank_sf": DYN_RANK.get(fid), "rank_1qb": DYN_RANK_1QB.get(fid)}


def player_values(p):
    """Redraft value (superflex and 1-QB) from ROS positional rank; dynasty value from overall value rank."""
    pos, ros, d = p["position"], ros_pos_rank(p), dynasty(p)
    return {"value": curve(ros, MODEL["half"][pos]), "value_1qb": curve(ros, MODEL["half_1qb"][pos]),
            "dvalue": curve(d["rank_sf"], MODEL["dyn_half"]), "dvalue_1qb": curve(d["rank_1qb"], MODEL["dyn_half"])}


# ---------------------------------------------------------------- consensus pickups (heat)
def slide(i, window):
    """Credit for the (i+1)th entry: 1.0 for the first, sliding to 0.3 at the window's end, flat after."""
    return CREDIT["top"] - (CREDIT["top"] - CREDIT["floor"]) * min(i, window - 1) / (window - 1)


def credited(plist, ranking_type):
    """(entry, credit, rank shown) for the entries kept from one outlet's list. At most CREDIT['cap'] per list.
    overall: credit slides over a fixed top 25 (not the list's own length, so long lists don't dominate).
    by_position: ranks restart per position; entries are taken round-robin by within-position rank (so a
      95-player list keeps its top ~7 at each position) and credited by within-position rank over 8.
    unranked: flat credit for the first 30 in page order."""
    cap = CREDIT["cap"]
    order = sorted(enumerate(plist), key=lambda t: (t[1].get("rank") if t[1].get("rank") is not None else 999, t[0]))
    if ranking_type == "unranked":
        return [(x, CREDIT["flat"], i + 1) for i, (_, x) in enumerate(sorted(enumerate(plist), key=lambda t: t[0])[:cap])]
    if ranking_type == "by_position":
        per = {}
        for _, x in order:
            per.setdefault((x.get("pos") or "").upper(), []).append(x)
        rows, j = [], 0
        while len(rows) < cap and any(j < len(v) for v in per.values()):
            for pos in ("QB", "RB", "WR", "TE"):
                v = per.get(pos, [])
                if j < len(v) and len(rows) < cap:
                    rows.append((v[j], slide(j, CREDIT["pos_window"]), j + 1))
            j += 1
        return rows
    return [(x, slide(i, CREDIT["window"]), i + 1) for i, (_, x) in enumerate(order[:cap])]


source_meta, mentions = [], {}


def mention(p):
    return mentions.setdefault(p["sleeper_id"], {"p": p, "hits": [], "faab": [], "sf_faab": [], "trend": None})


for key in SOURCE_ORDER:
    path = os.path.join(ROOT, "sources", f"{key}.json")
    if not os.path.exists(path):
        source_meta.append({"key": key, "source": key, "status": "missing", "count": 0, "kept": 0})
        continue
    d = json.load(open(path))
    plist = [x for x in d.get("players", []) if (x.get("pos") or "").upper() in OFFENSE]
    rtype = d.get("ranking_type") or "overall"
    rem = d.get("budget_basis") == "remaining"
    meta = {"key": key, "source": d.get("source", key), "url": d.get("url"), "week": d.get("week"),
            "status": d.get("status"), "ranking_type": rtype, "count": len(plist), "unmatched": [],
            "basis": "remaining" if rem else "total"}
    kept = credited(plist, rtype)
    meta["kept"] = len(kept)
    kept_ids = {id(x) for x, _, _ in kept}
    for x in plist:  # bids are read from the whole list; credit only from the kept part
        p = lookup(x["name"], (x.get("pos") or "").upper(), x.get("team"))
        if not p:
            meta["unmatched"].append(x["name"])
            continue
        m = mention(p)
        lo, hi = num(x.get("faab_pct_low")), num(x.get("faab_pct_high"))
        if (lo is not None or hi is not None) and not any(f["src"] == meta["source"] for f in m["faab"]):
            m["faab"].append({"src": meta["source"], "lo": lo if lo is not None else hi, "hi": hi if hi is not None else lo, "rem": rem})
        slo, shi = num(x.get("sf_faab_pct_low")), num(x.get("sf_faab_pct_high"))
        if (slo is not None or shi is not None) and not any(f["src"] == meta["source"] for f in m["sf_faab"]):
            m["sf_faab"].append({"src": meta["source"], "lo": slo if slo is not None else shi, "hi": shi if shi is not None else slo, "rem": rem})
    for x, credit, shown in kept:
        p = lookup(x["name"], (x.get("pos") or "").upper(), x.get("team"))
        if not p:
            continue
        m = mention(p)
        if any(h["key"] == key for h in m["hits"]):
            continue
        m["hits"].append({"key": key, "source": meta["source"], "pos_in_list": shown, "credit": round(credit, 3)})
    source_meta.append(meta)

# Sleeper trending adds: what managers are actually adding. Credited like one more ranked list.
TREND_META = {"status": "missing", "fetched_at": None, "lookback_hours": None, "count": 0, "not_in_db": 0}
_tp = os.path.join(ROOT, "data", "trending_add.json")
if os.environ.get("TRENDING", "1") != "0" and os.path.exists(_tp):
    _t = json.load(open(_tp))
    try:
        _age = (datetime.now(timezone.utc) - datetime.fromisoformat(_t["fetched_at"])).total_seconds() / 3600
    except (KeyError, ValueError):
        _age = 1e9
    TREND_META.update(fetched_at=_t.get("fetched_at"), lookback_hours=_t.get("lookback_hours"))
    if _age > 48:
        TREND_META["status"] = "stale"
    else:
        TREND_META["status"] = "ok"
        rank = 0
        for x in _t.get("players", []):
            pid = str(x.get("player_id", ""))
            if not pid.isdigit():  # team defenses come through as "DET", "NO"
                continue
            p = by_sleeper.get(pid)
            if not p:
                TREND_META["not_in_db"] += 1
                continue
            if p["position"] not in OFFENSE:
                continue
            rank += 1
            if rank > CREDIT["cap"]:
                break
            m = mention(p)
            m["trend"] = {"rank": rank, "count": x.get("count")}
            m["hits"].append({"key": "trending", "source": "Sleeper trending", "pos_in_list": rank,
                              "credit": round(slide(rank - 1, CREDIT["window"]), 3)})
        TREND_META["count"] = rank if rank <= CREDIT["cap"] else CREDIT["cap"]
elif os.environ.get("TRENDING", "1") == "0":
    TREND_META["status"] = "off"

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
        m = mention(p)
        lo, hi = num(x.get("faab_pct_low")), num(x.get("faab_pct_high"))
        if lo is not None or hi is not None:
            m["faab"] = [f for f in m["faab"] if f["src"] != d.get("source")]
            m["faab"].append({"src": d.get("source"), "lo": lo if lo is not None else hi, "hi": hi if hi is not None else lo, "rem": rem})
        slo, shi = num(x.get("sf_faab_pct_low")), num(x.get("sf_faab_pct_high"))
        if slo is not None or shi is not None:
            m["sf_faab"] = [f for f in m["sf_faab"] if f["src"] != d.get("source")]
            m["sf_faab"].append({"src": d.get("source"), "lo": slo if slo is not None else shi, "hi": shi if shi is not None else slo, "rem": rem})


def faab_pool(pos, m):
    """The bids a player's estimate is built from. QB: every superflex-specific bid. Others: Faablab if
    present, else every published FAAB range."""
    if pos == "QB":
        return m["sf_faab"], "superflex"
    fl = [f for f in m["faab"] if "faablab" in (f["src"] or "").lower()]
    return (fl, "faablab") if fl else (m["faab"], "published")


def estimate(pool, basis, rem_frac=None, cap_pct=None, mult=1.0):
    """Median bid range, as % of total budget.

    Some outlets (RotoBaller's FAAB guide, Fantasy Squawk) quote % of the budget you have LEFT. With
    rem_frac (this league's remaining / total) those are converted to % of total before the median; then
    the league's calibration multiplier is applied and the result is capped at what you have left. Without
    rem_frac (the league-agnostic number), the %-of-total bids are used when there are any; otherwise the
    %-of-remaining bids are shown as such."""
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
    lo = st.median(u[0] for u in use) * mult
    hi = st.median(u[1] for u in use) * mult
    capped = False
    if cap_pct is not None and hi > cap_pct:
        lo, hi, capped = min(lo, cap_pct), cap_pct, True
    return {"lo": r1h(lo), "hi": r1h(hi), "mid": r1h((lo + hi) / 2), "n": len(srcs),
            "basis": basis, "of_remaining": of_remaining, "capped": capped, "approx": False,
            "from": sorted({f["src"] for f in srcs})}


def scale_est(e, cap_pct=None, mult=1.0):
    """A league bid from an approximate (filled-in) general bid: % of total, x calibration, capped."""
    if not e:
        return None
    lo, hi = e["lo"] * mult, e["hi"] * mult
    capped = False
    if cap_pct is not None and hi > cap_pct:
        lo, hi, capped = min(lo, cap_pct), cap_pct, True
    return {**e, "lo": r1h(lo), "hi": r1h(hi), "mid": r1h((lo + hi) / 2), "capped": capped}


# ---------------------------------------------------------------- leagues
leagues = []
for key, fn in LEAGUE_FILES:
    path = os.path.join(ROOT, "data", fn)
    if os.path.exists(path):
        L = json.load(open(path))
        L["key"] = key
        leagues.append(L)

# Injury designations from the Sleeper sync, used only when under 48 hours old. Out-type designations
# mean the player can't start this week; Questionable is shown but still counts as ready.
OUT_STATUSES = {"Out", "Doubtful", "IR", "PUP", "Sus", "NA", "DNR", "COV"}
INJURIES, INJURIES_AT = {}, None
_ip = os.path.join(ROOT, "data", "sleeper_injuries.json")
if os.path.exists(_ip):
    _d = json.load(open(_ip))
    try:
        _age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(_d["fetched_at"])).total_seconds() / 3600
    except (KeyError, ValueError):
        _age_h = 1e9
    if _age_h <= 48:
        INJURIES, INJURIES_AT = _d.get("players", {}), _d["fetched_at"]


def injury_of(sid):
    """("out" | "questionable" | None, label) from the Sleeper designation."""
    i = INJURIES.get(sid)
    if not i:
        return None, None
    inj = i.get("injury_status")
    if inj in OUT_STATUSES:
        return "out", inj
    if i.get("status") == "Practice Squad":
        return "out", "practice squad"
    if inj == "Questionable":
        return "questionable", "Q"
    return None, None


bids = {}
bp = os.path.join(ROOT, "data", "sleeper_bids.json")
if os.path.exists(bp):
    bids = json.load(open(bp)).get("leagues", {})


def flex_share(rp):
    c = {k: rp.count(k) for k in ("QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX")}
    c["FLEX"] += rp.count("REC_FLEX") + rp.count("WRRB_FLEX")  # same as logic.js
    need = {"QB": c["QB"] + 0.9 * c["SUPER_FLEX"],
            "RB": c["RB"] + 0.45 * c["FLEX"] + 0.04 * c["SUPER_FLEX"],
            "WR": c["WR"] + 0.45 * c["FLEX"] + 0.04 * c["SUPER_FLEX"],
            "TE": c["TE"] + 0.10 * c["FLEX"] + 0.02 * c["SUPER_FLEX"]}
    slots = {"QB": c["QB"] + c["SUPER_FLEX"], "RB": c["RB"], "WR": c["WR"], "TE": c["TE"]}
    return need, slots, c["FLEX"]


def roster_needs(L):
    """Lineup check. A player is startable if his rest-of-season positional rank is inside the league's
    starter range. Of those, a player isn't ready this week if he is (in this order): on a bye, in an IR or
    taxi slot, designated Out/Doubtful/IR/PUP/suspended in Sleeper, or missing from this week's rankings
    (only when the weekly ranks are for TARGET_WEEK). logic.js rosterNeeds() is the same function."""
    need, slots, nflex = flex_share(L["roster_positions"])
    R = L["rosters"][str(L["my_roster_id"])]
    mine = R["players"]
    stashed = set(R.get("reserve") or []) | set(R.get("taxi") or [])
    out = []
    flex_surplus = 0
    for pos in ("QB", "RB", "WR", "TE"):
        thresh = need[pos] * L["teams"]
        group = []
        for sid in mine:
            p = by_sleeper.get(sid)
            if not p or p["position"] != pos:
                continue
            group.append({"sid": sid, "name": p["name"], "ros": ros_pos_rank(p), "weekly": weekly_pos_rank(p),
                          "bye": bye_week(p)})
        startable = [g for g in group if g["ros"] is not None and g["ros"] <= thresh]
        on_bye, slot, injured, questionable, maybe_out = [], [], [], [], []
        for g in startable:
            kind, label = injury_of(g["sid"])
            if g["bye"] == TARGET_WEEK:
                on_bye.append(g["name"])
            elif g["sid"] in stashed:
                slot.append(g["name"])
            elif kind == "out":
                injured.append(f"{g['name']} ({label})")
            elif g["weekly"] is None:
                # in ROS startable range but missing from this week's rankings
                maybe_out.append(g["name"])
            elif kind == "questionable":
                questionable.append(g["name"])
        avail = len(startable) - len(on_bye) - len(slot) - len(injured) - len(maybe_out)
        req = slots[pos]
        level = "need" if avail < req else ("thin" if avail == req else "ok")
        flex_surplus += max(0, avail - req) if pos != "QB" else 0
        out.append({"pos": pos, "level": level, "startable": len(startable), "available_next_week": avail,
                    "slots": req, "on_bye": on_bye, "in_slot": slot, "injured": injured,
                    "questionable": questionable, "maybe_out": maybe_out, "threshold": round(thresh)})
    if flex_surplus < nflex:
        out.append({"pos": "FLEX", "level": "need" if flex_surplus < nflex - 1 else "thin",
                    "startable": flex_surplus, "available_next_week": flex_surplus, "slots": nflex,
                    "on_bye": [], "in_slot": [], "injured": [], "questionable": [], "maybe_out": [],
                    "threshold": None})
    return out


def season_claims(L):
    """Paid, in-season claims on offensive players: (claim, player row, % of budget)."""
    b = bids.get(L["league_id"])
    if not b:
        return []
    budget = b.get("budget") or L["budget"]
    out = []
    for c in b.get("claims", []):
        if not c.get("bid") or (c.get("created") or 0) < SEASON_START_MS:
            continue  # skip $0 claims and offseason/startup claims stored under week 1
        p = by_sleeper.get(str(c["player_id"]))
        if not p or p["position"] not in OFFENSE:
            continue
        out.append((c, p, round(100 * c["bid"] / budget, 1)))
    return out


def by_pos_stats(claims):
    by_pos = {}
    for pos in ("QB", "RB", "WR", "TE"):
        v = sorted(c["pct"] for c in claims if c["pos"] == pos)
        if v:
            by_pos[pos] = {"n": len(v), "median": round(st.median(v), 1), "max": max(v),
                           "p75": round(v[int(0.75 * (len(v) - 1))], 1)}
    return by_pos


def bid_history(L):
    claims = [{"player": p["name"], "pos": p["position"], "pct": pct, "week": c.get("week"),
               "mine": str(c.get("roster_id")) == str(L["my_roster_id"])} for c, p, pct in season_claims(L)]
    return {"this_season": {"season": (bids.get(L["league_id"]) or {}).get("season"), "n_paid_claims": len(claims),
                            "by_pos": by_pos_stats(claims), "top": sorted(claims, key=lambda c: -c["pct"])[:8]}} if claims else {}


# ---------------------------------------------------------------- bid calibration from past boards
# Each run saves the week's general bids (% of total, published only) to HISTORY_DIR/weekNN.json. A
# winning claim is matched to the latest board made before it (Sleeper: by timestamp, within 8 days;
# Yahoo: same week label). The league's multiplier is the median of (winning bid / board bid), shrunk
# toward 1 with few claims: 1 + (median - 1) * n / (n + 6), kept between 0.5 and 2.5. logic.js calibrate().
HISTORY = []
for _hp in sorted(glob.glob(os.path.join(HISTORY_DIR, "week*.json"))):
    _h = json.load(open(_hp))
    if int(_h.get("week", 0)) >= SOURCE_WEEK:
        continue  # this week's own board can't calibrate itself
    HISTORY.append({"week": int(_h["week"]), "at": int(datetime.fromisoformat(_h["generated_at"]).timestamp() * 1000),
                    "bids": _h.get("bids", {})})
HISTORY.sort(key=lambda h: h["at"])


def r2h(x):
    return math.floor(x * 100 + 0.5) / 100


def calibrate(pairs):
    C = MODEL["calib"]
    ratios = [a / p for a, p in pairs if p and p >= C["min_pred"]]
    n = len(ratios)
    if n < C["min_claims"]:
        return {"mult": 1.0, "n": n, "raw": None}
    raw = st.median(ratios)
    mult = min(C["hi"], max(C["lo"], 1 + (raw - 1) * n / (n + C["shrink"])))
    return {"mult": r2h(mult), "n": n, "raw": r2h(raw)}


def calib_sleeper(L):
    pairs = []
    for c, p, pct in season_claims(L):
        t = c.get("created") or 0
        h = next((h for h in reversed(HISTORY) if h["at"] <= t), None)
        if h and t - h["at"] <= MODEL["calib"]["max_days"] * 86400e3:
            pred = h["bids"].get(p["sleeper_id"])
            if pred:
                pairs.append((pct, pred))
    return calibrate(pairs)


def calib_yahoo(Y):
    pairs = []
    for c in Y.get("claims", []):
        if not c.get("bid"):
            continue
        h = next((h for h in HISTORY if h["week"] == c.get("week")), None)
        p = lookup(c["player"], c.get("pos"))
        if h and p and h["bids"].get(p["sleeper_id"]):
            pairs.append((100 * c["bid"] / Y["budget"], h["bids"][p["sleeper_id"]]))
    return calibrate(pairs)


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
                       "bid_history": bid_history(L), "calib": calib_sleeper(L), "league_id": L["league_id"],
                       "roster_positions": L["roster_positions"], "my_sids": sorted(mine),
                       "fetched_at": L.get("fetched_at"), "stale": False})
# Yahoo leagues: written during a MacBook run (read through Claude in Chrome), one file per league:
# data/yahoo_<key>.json = {"name", "budget", "teams", "my_remaining", "max_opp_remaining", "opp_remaining",
#   "my_roster": [names], "status": {name: available|taken|mine}, "claims": [{player, pos, bid, week, mine}],
#   "as_of_week": 9, "fetched_at": "2026-11-04T19:00-08:00"}
# Cloud-only runs keep the last file, so it is stale whenever as_of_week isn't this run's SOURCE_WEEK.
yahoo = []
for path in sorted(glob.glob(os.path.join(ROOT, "data", "yahoo_*.json"))):
    Y = json.load(open(path))
    Y["key"] = os.path.basename(path)[6:-5]
    Y["status_norm"] = {norm(k): v for k, v in Y.get("status", {}).items()}
    yahoo.append(Y)
    claims = [{"player": c["player"], "pos": c.get("pos", "?"), "pct": round(100 * c["bid"] / Y["budget"], 1),
               "week": c.get("week"), "mine": bool(c.get("mine"))} for c in Y.get("claims", []) if c.get("bid")]
    my_sids = sorted({p["sleeper_id"] for p in (lookup(n) for n in Y.get("my_roster", [])) if p})
    league_out.append({"key": Y["key"], "name": Y["name"], "platform": "Yahoo", "treat_as": "redraft",
                       "teams": Y.get("teams"),
                       "faab": {"budget": Y["budget"], "my_remaining": Y.get("my_remaining"),
                                "my_remaining_pct": round(100 * Y["my_remaining"] / Y["budget"], 1) if Y.get("my_remaining") is not None else None,
                                "max_opp_remaining_pct": round(100 * Y["max_opp_remaining"] / Y["budget"], 1) if Y.get("max_opp_remaining") is not None else None,
                                "teams_with_more": sum(1 for v in Y.get("opp_remaining", []) if v > (Y.get("my_remaining") or 0)) if Y.get("opp_remaining") else None,
                                "median_opp_remaining_pct": round(100 * st.median(Y["opp_remaining"]) / Y["budget"], 1) if Y.get("opp_remaining") else None},
                       "bid_history": {"this_season": {"n_paid_claims": len(claims), "by_pos": by_pos_stats(claims),
                                                       "top": sorted(claims, key=lambda c: -c["pct"])[:8]}} if claims else {},
                       "calib": calib_yahoo(Y), "roster_positions": YAHOO_LINEUP, "my_sids": my_sids,
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
    outlets = [h for h in m["hits"] if h["key"] != "trending"]
    heat = round(sum(h["credit"] for h in m["hits"]), 3)
    avail = {}
    for L in leagues:
        owned_ids, mine = rostered[L["key"]]
        avail[L["key"]] = "mine" if sid in mine else ("taken" if sid in owned_ids else "available")
    for Y in yahoo:
        avail[Y["key"]] = Y["status_norm"].get(norm(p["name"]), "unknown")
    pool, pool_basis = faab_pool(pos, m)
    std_pool = [f for f in m["faab"] if "faablab" not in (f["src"] or "").lower()]
    rows.append({"sleeper_id": sid, "name": p["name"], "pos": pos, "p": p,
                 "team": DISPLAY_TEAM.get(p["team"], p["team"]), "age": num(p.get("age")),
                 "mentions": len(outlets), "trend": m["trend"], "heat": heat,
                 "sources": [h["source"] for h in sorted(m["hits"], key=lambda h: h["pos_in_list"])],
                 "faab": estimate(pool, pool_basis), "std_faab": estimate(std_pool, "published") if pos == "QB" else None,
                 "faab_pool": pool, "faab_pool_basis": pool_basis,
                 "faab_inputs": m["faab"], "sf_faab_inputs": m["sf_faab"],
                 "availability": avail, "ros_pos_rank": ros_pos_rank(p), "ros_overall": ros_overall(p),
                 "weekly_pos_rank": weekly_pos_rank(p) if FP_WEEKLY or USE_MIRROR_WEEKLY else None,
                 "owned_pct": owned(p), "bye": bye_week(p), "dynasty": dynasty(p), **player_values(p)})
rows = [r for r in rows if r["mentions"] > 0 or r["trend"] or (r["faab"] and r["faab"]["mid"] >= 1)]

# Scores. Heat is scaled so the hottest player on the board is 100.
MAX_HEAT = max((r["heat"] for r in rows), default=1) or 1
VW, HW = MODEL["value_weight"], MODEL["heat_weight"]
for r in rows:
    r["heat_n"] = math.floor(100 * r["heat"] / MAX_HEAT + 0.5)  # whole number, 0-100
    r["score"] = r1h(VW * r["value"] + HW * r["heat_n"])
    r["score_1qb"] = r1h(VW * r["value_1qb"] + HW * r["heat_n"])
    signals = r["mentions"] + (1 if r["trend"] else 0)
    # One outlet and not trending (or trending outside the top 10): kept out of the main list
    r["deep"] = signals < 2 and not (r["trend"] and r["trend"]["rank"] <= 10)
# Fill missing bids, marked approximate ("~").
# 1. Superflex QB bid from a standard bid of 2%+: x the median superflex/standard ratio of QBs that have both.
_ratios = [r["faab"]["mid"] / r["std_faab"]["mid"] for r in rows
           if r["pos"] == "QB" and r["faab"] and r["std_faab"] and not r["faab"]["of_remaining"]
           and not r["std_faab"]["of_remaining"] and r["std_faab"]["mid"] >= 1]
SF_RATIO = round(st.median(_ratios), 2) if len(_ratios) >= 3 else None
for r in rows:
    if (r["pos"] == "QB" and not r["faab"] and r["std_faab"] and SF_RATIO and not r["std_faab"]["of_remaining"]
            and r["std_faab"]["mid"] >= 2):  # a 1% standard bid is a token bid; scaling it says little
        e = r["std_faab"]
        lo, hi = min(100, e["lo"] * SF_RATIO), min(100, e["hi"] * SF_RATIO)
        r["faab"] = {"lo": r1h(lo), "hi": r1h(hi), "mid": r1h((lo + hi) / 2), "n": e["n"], "basis": "sf_from_std",
                     "of_remaining": False, "capped": False, "approx": True, "from": e["from"]}
# 2. Anyone still without a bid: median bid of the 5 same-position players with the closest board score
#    (value + heat: what a player costs follows both how good and how hyped he is).
_known = [r for r in rows if r["faab"] and not r["faab"]["approx"] and not r["faab"]["of_remaining"]]
for r in rows:
    if r["faab"]:
        continue
    near = sorted((k for k in _known if k["pos"] == r["pos"]), key=lambda k: abs(k["score"] - r["score"]))[:5]
    if len(near) >= 3:
        mid = r1h(st.median(k["faab"]["mid"] for k in near))
        r["faab"] = {"lo": mid, "hi": mid, "mid": mid, "n": 0, "basis": "similar", "of_remaining": False,
                     "capped": False, "approx": True, "from": [k["name"] for k in near]}

rows.sort(key=lambda r: (-r["score"], -r["heat"]))
for i, r in enumerate(rows, 1):
    r["consensus_rank"] = i
for i, r in enumerate(sorted(rows, key=lambda r: (-r["score_1qb"], -r["heat"])), 1):
    r["rank_1qb"] = i


# ---------------------------------------------------------------- per league: "For you" and your bid
def for_you(LO):
    """For each board player in one league: the league score (redraft value from this league's starter depth,
    or dynasty value), plus half of how far he beats your worst starter at his position, plus a Need/Thin
    bonus. Returns {sid: {"fy", "gain", "over"}}. logic.js forYou() is the same."""
    need, _, _ = flex_share(LO["roster_positions"])
    teams = LO.get("teams") or 12
    half = {pos: max(1.0, need[pos] * teams) for pos in OFFENSE}
    dyn = LO["treat_as"] == "dynasty"

    def val(p):
        if dyn:
            return curve(dynasty(p)["rank_sf"], MODEL["dyn_half"])
        ros = ros_pos_rank(p)  # one decimal, as players.json carries it, so logic.js gets the same numbers
        return curve(round(ros, 1) if ros else None, half[p["position"]])

    bar = {}
    for pos in OFFENSE:
        mine = sorted(((val(p), p["name"]) for p in (by_sleeper.get(s) for s in LO["my_sids"]) if p and p["position"] == pos),
                      reverse=True)
        k = max(1, math.floor(need[pos] + 0.5))
        bar[pos] = mine[k - 1] if len(mine) >= k else (0, None)
    lv = {n["pos"]: n["level"] for n in LO.get("needs", [])}
    nb = MODEL["need_bonus"]
    out = {}
    for r in rows:
        pos = r["pos"]
        v = val(r["p"])
        gain = v - bar[pos][0]
        bonus = nb.get(lv.get(pos), 0) + (nb.get(lv.get("FLEX"), 0) / 2 if pos != "QB" else 0)
        fy = VW * v + HW * r["heat_n"] + MODEL["gain_weight"] * max(0, gain) + bonus
        out[r["sleeper_id"]] = {"fy": r1h(fy), "gain": r1h(gain), "over": bar[pos][1] if gain > 0 else None,
                                "empty": gain > 0 and bar[pos][1] is None}
    return out


FY = {LO["key"]: for_you(LO) for LO in league_out if "pending" not in LO}
for r in rows:
    pool, pool_basis = r["faab_pool"], r["faab_pool_basis"]
    r["league_faab"], r["for_you"] = {}, {}
    for LO in league_out:
        if "pending" in LO:
            continue
        r["for_you"][LO["key"]] = FY[LO["key"]][r["sleeper_id"]]
        fa = LO.get("faab") or {}
        if fa.get("my_remaining") is None or not fa.get("budget"):
            continue
        mult = (LO.get("calib") or {}).get("mult", 1.0)
        if r["faab"] and r["faab"]["approx"]:
            r["league_faab"][LO["key"]] = scale_est(r["faab"], fa["my_remaining_pct"], mult)
        else:
            r["league_faab"][LO["key"]] = estimate(pool, pool_basis, fa["my_remaining"] / fa["budget"], fa["my_remaining_pct"], mult)
for LO in league_out:
    if "pending" in LO:
        continue
    order = sorted(rows, key=lambda r: (-r["for_you"][LO["key"]]["fy"], r["consensus_rank"]))
    for i, r in enumerate(order, 1):
        r["for_you"][LO["key"]]["rank"] = i

ok_sources = [s for s in source_meta if s.get("status") in ("ok", "partial") and s["count"]]
PLAYERS_OUT = [{k: v for k, v in r.items() if k != "p"} for r in rows]
out = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    "source_week": SOURCE_WEEK, "target_week": TARGET_WEEK, "model": MODEL, "credit": CREDIT,
    "sf_ratio": SF_RATIO, "trending": TREND_META,
    "sources": source_meta, "faab_sources": faab_meta, "n_sources_used": len(ok_sources),
    "reference": {"fantasypros_ecr_date": ecr_date, "dynasty_values_date": values_date,
                  "weekly_ranks": WEEKLY_SOURCE, "weekly_ranks_week": WEEKLY_WEEK if WEEKLY_SOURCE else None,
                  "dynasty_values": "DynastyProcess superflex values (FantasyPros dynasty ECR)"},
    "leagues": league_out, "players": PLAYERS_OUT,
}
os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
json.dump(out, open(os.path.join(ROOT, "out", "dashboard.json"), "w"), indent=1)
# The weekly ranks this run used, for export_site.py (players.json): {"source", "week", "ranks": {fp id: pos rank}}
json.dump({"source": WEEKLY_SOURCE, "week": WEEKLY_WEEK if WEEKLY_SOURCE else None, "mirror": USE_MIRROR_WEEKLY,
           "ranks": {k: v.get("pos_rank") for k, v in FP_WEEKLY.items()},
           "byes": {k: v.get("bye") for k, v in FP_WEEKLY.items() if v.get("bye")}},
          open(os.path.join(ROOT, "out", "weekly_ranks.json"), "w"))

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
    t = ("~" + t) if f.get("approx") else t
    return t + " of left" if f.get("of_remaining") else t


BASIS_LABEL = {"superflex": "Superflex QB bid", "faablab": "Faablab", "published": "Published FAAB",
               "sf_from_std": "Estimated superflex bid", "similar": "Estimated bid"}


def fmt_basis(f, league=False, mult=1.0):
    if not f:
        return None
    if f["basis"] == "sf_from_std":
        t = f"Estimated superflex bid: standard bid ({', '.join(f['from'])}) x {SF_RATIO}, the typical superflex/standard ratio this week"
    elif f["basis"] == "similar":
        t = f"Estimated bid: no outlet printed one; median of the {f.get('pos', '')}s ranked closest ({', '.join(f['from'])})"
    else:
        t = BASIS_LABEL[f["basis"]] + f" ({', '.join(f['from'])})"
    if f.get("of_remaining"):
        t += ", % of the budget you have left"
    elif league:
        t += ", % of total" + ("" if f.get("approx") else "; %-of-remaining bids scaled to your FAAB left")
        if mult != 1.0:
            t += f"; x{mult:g} for what this league pays"
        if f.get("capped"):
            t += ", capped at what you have"
    return t


def heat_label(r):
    t = f"{r['mentions']} outlet{'s' if r['mentions'] != 1 else ''}"
    if r["trend"]:
        t += f" · trending #{r['trend']['rank']}"
    return t


def ros_label(r):
    return f"{r['pos']}{round(r['ros_pos_rank'])}" if r["ros_pos_rank"] else None


ds_pickups = []
AV = {"available": "Open", "taken": "Taken", "mine": "Yours", "unknown": "Unchecked"}
for r in rows:
    f = r["faab"]
    if f and f["basis"] == "similar":
        f["pos"] = r["pos"]
    row = {"rank": r["consensus_rank"], "player": r["name"], "pos": r["pos"], "team": r["team"],
           "mentions": r["mentions"], "score": r["score"], "value": r["value"], "heat": r["heat_n"],
           "heat_label": heat_label(r), "trend_rank": r["trend"]["rank"] if r["trend"] else None,
           "deep": "Yes" if r["deep"] else "", "sources": ", ".join(r["sources"]),
           "faab": fmt_range(f), "faab_mid": f["mid"] if f else None, "faab_basis": fmt_basis(f),
           "ros": ros_label(r), "ros_pos_rank": round(r["ros_pos_rank"]) if r["ros_pos_rank"] else None,
           "owned_pct": r["owned_pct"], "bye": r["bye"],
           "dyn_value": r["dynasty"]["value_sf"], "dyn_rank": round(r["dynasty"]["ecr_sf"]) if r["dynasty"]["ecr_sf"] else None}
    for k in league_keys:
        LO = next(L for L in league_out if L["key"] == k)
        row["avail_" + k] = AV[r["availability"].get(k, "unknown")]
        lf = r["league_faab"].get(k)
        if lf and lf.get("basis") == "similar":
            lf["pos"] = r["pos"]
        row["bid_" + k] = fmt_range(lf)
        row["bidmid_" + k] = lf["mid"] if lf else None
        row["bidbasis_" + k] = fmt_basis(lf, league=True, mult=(LO.get("calib") or {}).get("mult", 1.0))
        fy = r["for_you"][k]
        row["fy_" + k] = fy["rank"]
        row["up_" + k] = ("fills an empty spot" if fy["empty"] else f"+{fy['gain']:g} over {fy['over']}") if fy["over"] or fy["empty"] else ""
    row["open_in"] = sum(1 for k in league_keys if r["availability"].get(k) == "available")
    row["open_leagues"] = ", ".join(SHORT.get(k, k) for k in league_keys if r["availability"].get(k) == "available")
    ds_pickups.append(row)

ds_leagues, ds_needs, ds_bidpos, ds_topbids = [], [], [], []
for L in league_out:
    fa = L.get("faab") or {}
    cal = L.get("calib") or {}
    ds_leagues.append({"league": L["key"], "name": SHORT.get(L["key"], L["name"]), "full_name": L["name"],
                       "platform": L["platform"], "type": L["treat_as"].title(), "teams": L.get("teams"),
                       "budget": fa.get("budget"), "my_remaining_pct": fa.get("my_remaining_pct"),
                       "max_opp_remaining_pct": fa.get("max_opp_remaining_pct"),
                       "teams_with_more": fa.get("teams_with_more"),
                       "median_opp_remaining_pct": fa.get("median_opp_remaining_pct"),
                       "calib_mult": cal.get("mult"), "calib_n": cal.get("n"),
                       "calib": (f"Pays about {cal['mult']:g}x published ({cal['n']} claims)" if cal.get("raw") is not None
                                 else f"Not enough history yet ({cal.get('n', 0)} of {MODEL['calib']['min_claims']} claims)") if cal else None,
                       "status": L.get("pending") or "Loaded",
                       "stale": bool(L.get("stale")),
                       "as_of": (f"Week {L['as_of_week']} data" if L.get("as_of_week") else "last MacBook run") if L.get("stale") else None,
                       "fetched_at": L.get("fetched_at"),
                       "flags": sum(1 for n in L.get("needs", []) if n["level"] != "ok")})
    for n in L.get("needs", []):
        detail = []
        if n["on_bye"]:
            detail.append(f"Week {TARGET_WEEK} bye: " + ", ".join(n["on_bye"]))
        if n.get("in_slot"):
            detail.append("In IR/taxi slot: " + ", ".join(n["in_slot"]))
        if n.get("injured"):
            detail.append("Out: " + ", ".join(n["injured"]))
        if n["maybe_out"]:
            detail.append("Not ranked this week: " + ", ".join(n["maybe_out"]))
        if n.get("questionable"):
            detail.append("Questionable: " + ", ".join(n["questionable"]))
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
      "missing": "Not run", "gated": "Needs your own bids", "stale": "Stale (over 48 h old)", "off": "Off"}
ds_sources = []
for s in source_meta:
    ds_sources.append({"source": s["source"], "role": "Waiver pickups", "status": ST.get(s.get("status"), s.get("status")),
                       "players": s["count"], "week": s.get("week"), "url": s.get("url")})
ds_sources.append({"source": "Sleeper trending adds", "role": f"Heat ({TREND_META.get('lookback_hours') or 48} h adds)",
                   "status": ST.get(TREND_META["status"], TREND_META["status"]), "players": TREND_META["count"] or None,
                   "week": None, "url": "https://sleeper.com"})
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
ds_sources.append({"source": "Sleeper injury report", "role": "Lineup check: Out, IR, Questionable",
                   "status": "Loaded" if INJURIES_AT else "Not run", "players": len(INJURIES) or None,
                   "week": None, "url": "https://sleeper.com"})
ds_sources.append({"source": "DynastyProcess / FantasyPros ECR", "role": "Value: ROS ranks, dynasty values, weekly ranks",
                   "status": "Loaded", "players": None, "week": None, "url": "https://github.com/dynastyprocess/data"})

ds_run = [{"source_week": SOURCE_WEEK, "target_week": TARGET_WEEK,
           "generated": datetime.now(PT).strftime("%a %b %-d, %-I:%M %p %Z"),
           "sources_used": len(ok_sources), "sources_total": len(SOURCE_ORDER),
           "ecr_date": ecr_date, "values_date": values_date,
           "dynasty_leagues": ", ".join(SHORT.get(k, k) for k in dyn_keys),
           "weekly_ranks": WEEKLY_SOURCE or f"stale (latest is week {WEEKLY_WEEK}); not-ranked check skipped",
           "injuries": f"Sleeper, {INJURIES_AT}" if INJURIES_AT else "none (sync missing or over 48 h old)",
           "trending": f"{TREND_META['status']}" + (f", {TREND_META['fetched_at']}" if TREND_META.get("fetched_at") else ""),
           "sf_ratio": SF_RATIO, "deep_cuts": sum(1 for r in rows if r["deep"]),
           "league_keys": ",".join(league_keys)}]

for name, data in (("pickups", ds_pickups), ("leagues", ds_leagues), ("needs", ds_needs),
                   ("bid_by_pos", ds_bidpos), ("top_bids", ds_topbids), ("sources", ds_sources), ("run", ds_run)):
    json.dump(data, open(os.path.join(ROOT, "out", f"{name}.json"), "w"), indent=1)
print(f"weekly ranks: {WEEKLY_SOURCE or 'STALE (latest week ' + str(WEEKLY_WEEK) + ')'}")
print(f"injuries: {len(INJURIES)} flagged, from {INJURIES_AT or 'NOT LOADED'}")
print(f"trending: {TREND_META['status']} n={TREND_META['count']} not_in_db={TREND_META['not_in_db']}")
print(f"players={len(rows)} deep_cuts={sum(1 for r in rows if r['deep'])} sf_ratio={SF_RATIO} "
      f"est_bids={sum(1 for r in rows if r['faab'] and r['faab']['approx'])} history_weeks={len(HISTORY)} "
      f"sources_ok={len(ok_sources)}/12 leagues={len(leagues)} yahoo={len(yahoo)} bids={'yes' if bids else 'no'}")
for L in league_out:
    if L.get("calib"):
        print(f"  calib {L['key']:<10} {L['calib']}")
for s in source_meta:
    print(f"  {s['source']:<24} {s.get('status')!s:<8} {s.get('ranking_type')!s:<12} n={s['count']:<3} kept={s.get('kept', 0):<3} unmatched={s.get('unmatched', [])[:5]}")
for s in faab_meta:
    print(f"  FAAB {s['source']!s:<40} {s.get('status')!s:<8} n={s['count']:<3} unmatched={s.get('unmatched', [])[:5]}")
