// League math for the waiver board. Pure functions: no fetching, no DOM, so Node can test them.
// A port of the league half of build.py (claude/waiver/build.py in the Fantasy Football Waiver Manager project).

export const OFFENSE = ['QB', 'RB', 'WR', 'TE'];

// players.json rows: [name, pos, team, ros_pos_rank, weekly_pos_rank, bye, dyn_value, dyn_value_1qb, dyn_rank, dyn_rank_1qb]
export const P = { name: 0, pos: 1, team: 2, ros: 3, weekly: 4, bye: 5, dyn: 6, dyn1: 7, dynR: 8, dynR1: 9 };

export function isSuperflex(league) {
  const rp = league.roster_positions || [];
  return rp.includes('SUPER_FLEX') || rp.filter((x) => x === 'QB').length >= 2;
}

export const isFaab = (league) => Number((league.settings || {}).waiver_type) === 2;

// Sleeper league type: 0 redraft, 1 keeper, 2 dynasty. Keepers are treated as redraft.
export const treatAs = (league) => (Number((league.settings || {}).type) === 2 ? 'dynasty' : 'redraft');

export function findMyRoster(rosters, userId) {
  return rosters.find((r) => r.owner_id === userId || (r.co_owners || []).includes(userId)) || null;
}

function flexShare(rp) {
  const c = {};
  for (const k of ['QB', 'RB', 'WR', 'TE', 'FLEX', 'SUPER_FLEX']) c[k] = rp.filter((x) => x === k).length;
  // REC_FLEX (WR/TE) and WRRB_FLEX count as flex for these purposes
  c.FLEX += rp.filter((x) => x === 'REC_FLEX' || x === 'WRRB_FLEX').length;
  const need = {
    QB: c.QB + 0.9 * c.SUPER_FLEX,
    RB: c.RB + 0.45 * c.FLEX + 0.04 * c.SUPER_FLEX,
    WR: c.WR + 0.45 * c.FLEX + 0.04 * c.SUPER_FLEX,
    TE: c.TE + 0.1 * c.FLEX + 0.02 * c.SUPER_FLEX,
  };
  const slots = { QB: c.QB + c.SUPER_FLEX, RB: c.RB, WR: c.WR, TE: c.TE };
  return { need, slots, nflex: c.FLEX };
}

// Sleeper injury designations (sync/injuries.json). Out-type ones mean the player can't start this week;
// Questionable is shown but still counts as ready. Same lists as build.py.
export const OUT_STATUSES = ['Out', 'Doubtful', 'IR', 'PUP', 'Sus', 'NA', 'DNR', 'COV'];
export function injuryOf(injuries, sid) {
  const i = injuries && injuries[sid];
  if (!i) return [null, null];
  if (OUT_STATUSES.includes(i.injury_status)) return ['out', i.injury_status];
  if (i.status === 'Practice Squad') return ['out', 'practice squad'];
  if (i.injury_status === 'Questionable') return ['questionable', 'Q'];
  return [null, null];
}

// Lineup check (build.py roster_needs). A player is startable if his rest-of-season positional rank is
// inside the league's starter range. Of those, a player isn't ready this week if he is (in this order):
// on a bye, in an IR or taxi slot, designated Out/Doubtful/IR/PUP/suspended, or missing from this week's
// rankings (only when the weekly ranks are for the target week).
export function rosterNeeds({ rosterPositions, teams, myPlayers, players, targetWeek, weeklyOk, stashed = [], injuries = {} }) {
  const { need, slots, nflex } = flexShare(rosterPositions);
  const inSlot = new Set(stashed);
  const out = [];
  let flexSurplus = 0;
  for (const pos of OFFENSE) {
    const thresh = need[pos] * teams;
    const group = [];
    for (const sid of myPlayers) {
      const p = players[sid];
      if (!p || p[P.pos] !== pos) continue;
      group.push({ sid, name: p[P.name], ros: p[P.ros], weekly: p[P.weekly], bye: p[P.bye] });
    }
    const startable = group.filter((g) => g.ros !== null && g.ros !== undefined && g.ros <= thresh);
    const onBye = [], slot = [], injured = [], questionable = [], maybeOut = [];
    for (const g of startable) {
      const [kind, label] = injuryOf(injuries, g.sid);
      if (g.bye === targetWeek) onBye.push(g.name);
      else if (inSlot.has(g.sid)) slot.push(g.name);
      else if (kind === 'out') injured.push(`${g.name} (${label})`);
      else if (weeklyOk && (g.weekly === null || g.weekly === undefined)) maybeOut.push(g.name);
      else if (kind === 'questionable') questionable.push(g.name);
    }
    const avail = startable.length - onBye.length - slot.length - injured.length - maybeOut.length;
    const req = slots[pos];
    let level = 'ok';
    if (avail < req) level = 'need';
    else if (avail === req || (pos !== 'QB' && avail < req + 1)) level = 'thin';
    if (pos !== 'QB') flexSurplus += Math.max(0, avail - req);
    out.push({ pos, level, startable: startable.length, ready: avail, slots: req, onBye, inSlot: slot, injured,
      questionable, maybeOut, threshold: Math.round(thresh) });
  }
  if (flexSurplus < nflex) {
    out.push({ pos: 'FLEX', level: flexSurplus < nflex - 1 ? 'need' : 'thin', startable: flexSurplus, ready: flexSurplus,
      slots: nflex, onBye: [], inSlot: [], injured: [], questionable: [], maybeOut: [], threshold: null });
  }
  return out;
}

const median = (v) => {
  const s = [...v].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};
const r1 = (x) => Number(x.toFixed(1));  // same half-way behaviour as Python round(x, 1)

// One league's bid, as % of total budget (build.py's estimate() with rem_frac). Each input is
// [lo, hi, rem] from board.json; rem = 1 means the outlet quoted % of the budget you have LEFT, so it is
// scaled by what this team has left before the median. Then x this league's calibration multiplier, capped
// at what's left. With no inputs, `est` ([lo, hi], an estimated bid, % of total) is used instead (scale_est).
const r1h = (x) => Math.floor(x * 10 + 0.5) / 10;  // halves up, same arithmetic as build.py's r1h
const r2h = (x) => Math.floor(x * 100 + 0.5) / 100;
export function leagueBid(inputs, remFrac, capPct, mult = 1, est = null) {
  let lo, hi, n, approx = false;
  if (inputs && inputs.length) {
    const k = (v, rem) => (rem ? v * remFrac : v);
    lo = median(inputs.map((x) => k(x[0], x[2]))) * mult;
    hi = median(inputs.map((x) => k(x[1], x[2]))) * mult;
    n = inputs.length;
  } else if (est) {
    lo = est[0] * mult; hi = est[1] * mult; n = 0; approx = true;
  } else return null;
  let capped = false;
  if (capPct !== null && capPct !== undefined && hi > capPct) { lo = Math.min(lo, capPct); hi = capPct; capped = true; }
  return { lo: r1h(lo), hi: r1h(hi), mid: r1h((lo + hi) / 2), capped, n, approx };
}

export const fmtBid = (b) => (!b ? null : (b.approx ? '~' : '') + (b.lo === b.hi ? `${b.lo}%` : `${b.lo}–${b.hi}%`));

// ---------------------------------------------------------------- ranking model (build.py MODEL, via board.model)
// 0-100 value: 100 * 0.5 ** (rank / half); no rank -> model.missing_value. build.py curve().
export function curve(rank, half, model) {
  if (rank === null || rank === undefined) return model.missing_value;
  return r1h(100 * 0.5 ** (rank / half));
}

// "For you" (build.py for_you): league score (redraft value from this league's starter depth, or dynasty
// value) + half of how far a player beats your worst starter at his position + a Need/Thin bonus.
// Returns {sid: {fy, gain, over, empty, rank}} for every board pickup.
export function forYou({ rosterPositions, teams, dynasty, sf, myPlayers, players, pickups, needs, model }) {
  const { need } = flexShare(rosterPositions);
  const half = Object.fromEntries(OFFENSE.map((pos) => [pos, Math.max(1, need[pos] * teams)]));
  const val = (p) => (dynasty ? curve(p[sf ? P.dynR : P.dynR1], model.dyn_half, model) : curve(p[P.ros], half[p[P.pos]], model));
  const bar = {};
  for (const pos of OFFENSE) {
    const mine = myPlayers.map((sid) => players[sid]).filter((p) => p && p[P.pos] === pos)
      .map((p) => [val(p), p[P.name]])
      .sort((a, b) => (b[0] - a[0]) || (a[1] < b[1] ? 1 : a[1] > b[1] ? -1 : 0));
    const k = Math.max(1, Math.floor(need[pos] + 0.5));
    bar[pos] = mine.length >= k ? mine[k - 1] : [0, null];
  }
  const lv = Object.fromEntries((needs || []).map((n) => [n.pos, n.level]));
  const nb = model.need_bonus;
  const out = {};
  for (const r of pickups) {
    const p = players[r.sid];
    if (!p) continue;
    const v = val(p);
    const gain = v - bar[r.pos][0];
    const bonus = (nb[lv[r.pos]] || 0) + (r.pos !== 'QB' ? (nb[lv.FLEX] || 0) / 2 : 0);
    const fy = model.value_weight * v + model.heat_weight * r.heat + model.gain_weight * Math.max(0, gain) + bonus;
    out[r.sid] = { fy: r1h(fy), gain: r1h(gain), over: gain > 0 ? bar[r.pos][1] : null, empty: gain > 0 && bar[r.pos][1] === null };
  }
  const order = pickups.filter((r) => out[r.sid]).sort((a, b) => (out[b.sid].fy - out[a.sid].fy) || (a.rank - b.rank));
  order.forEach((r, i) => { out[r.sid].rank = i + 1; });
  return out;
}

export const upLabel = (f) => (!f ? '' : f.empty ? 'fills an empty spot' : f.over ? `+${f.gain} over ${f.over}` : '');

// What a league pays versus the published bids (build.py calibrate). Each paid claim is matched to the latest
// board made before it (within model.calib.max_days); ratio = winning % / that board's bid. Median ratio,
// shrunk toward 1 with few claims, kept within [lo, hi]. 1-QB leagues compare QBs with standard bids.
export function calibrate({ claims, budget, players, history, sf, seasonStartMs, model }) {
  const C = model.calib;
  const ratios = [];
  for (const c of claims) {
    if (!c.bid || (c.created || 0) < seasonStartMs) continue;
    const p = players[String(c.player_id)];
    if (!p || !OFFENSE.includes(p[P.pos])) continue;
    let h = null;
    for (const x of history || []) if (x.at <= c.created) h = x;  // history is oldest first
    if (!h || c.created - h.at > C.max_days * 86400e3) continue;
    const pred = (!sf && p[P.pos] === 'QB' ? h.bids_1qb : h.bids)[String(c.player_id)];
    if (pred && pred >= C.min_pred) ratios.push(r1((100 * c.bid) / budget) / pred);
  }
  const n = ratios.length;
  if (n < C.min_claims) return { mult: 1, n, raw: null };
  const raw = median(ratios);
  const mult = Math.min(C.hi, Math.max(C.lo, 1 + ((raw - 1) * n) / (n + C.shrink)));
  return { mult: r2h(mult), n, raw: r2h(raw) };
}

export function faabPosition(rosters, myRosterId, budget) {
  const rem = new Map(rosters.map((r) => [r.roster_id, Math.max(0, budget - Math.max(0, Number((r.settings || {}).waiver_budget_used) || 0))]));
  const mine = rem.get(myRosterId);
  const others = [...rem].filter(([k]) => k !== myRosterId).map(([, v]) => v);
  return {
    budget, myRemaining: mine, myRemainingPct: r1((100 * mine) / budget),
    maxOppRemainingPct: others.length ? r1((100 * Math.max(...others)) / budget) : null,
    teamsWithMore: others.filter((v) => v > mine).length,
    medianOppRemainingPct: others.length ? r1((100 * median(others)) / budget) : null,
  };
}

// Completed waiver claims from /league/<id>/transactions/<week> results, one row per added player.
export function claimsFromTransactions(txByWeek) {
  const out = [];
  for (const [week, txs] of Object.entries(txByWeek)) {
    for (const t of txs || []) {
      if (t.type !== 'waiver' || t.status !== 'complete') continue;
      for (const [pid, rid] of Object.entries(t.adds || {})) {
        out.push({ week: Number(t.leg ?? week), player_id: pid, bid: (t.settings || {}).waiver_bid ?? null,
          roster_id: rid, created: t.created });
      }
    }
  }
  return out;
}

// Winning bids this season as % of budget. $0 claims and offseason claims (Sleeper files those
// under week 1) are left out.
export function bidHistory({ claims, budget, myRosterId, players, seasonStartMs }) {
  const rows = [];
  for (const c of claims) {
    if (!c.bid || (c.created || 0) < seasonStartMs) continue;
    const p = players[String(c.player_id)];
    if (!p || !OFFENSE.includes(p[P.pos])) continue;
    rows.push({ player: p[P.name], pos: p[P.pos], pct: r1((100 * c.bid) / budget), week: c.week, mine: c.roster_id === myRosterId });
  }
  const byPos = {};
  for (const pos of OFFENSE) {
    const v = rows.filter((c) => c.pos === pos).map((c) => c.pct).sort((a, b) => a - b);
    if (v.length) byPos[pos] = { n: v.length, median: r1(median(v)), max: Math.max(...v), p75: r1(v[Math.floor(0.75 * (v.length - 1))]) };
  }
  return { nPaid: rows.length, byPos, top: [...rows].sort((a, b) => b.pct - a.pct).slice(0, 8) };
}

export function availability(sid, rosters, myRosterId) {
  for (const r of rosters) {
    if ((r.players || []).includes(sid)) return r.roster_id === myRosterId ? 'Yours' : 'Taken';
  }
  return 'Open';
}

// Everything the page shows for one league.
export function summarizeLeague({ league, rosters, txByWeek, userId, players, board, injuries = {} }) {
  const my = findMyRoster(rosters, userId);
  const faab = isFaab(league);
  const budget = Number((league.settings || {}).waiver_budget) || 100;
  const teams = Number((league.settings || {}).num_teams) || rosters.length;
  const weeklyOk = board.weekly_ranks_week === board.target_week;
  const base = { id: league.league_id, name: league.name, type: treatAs(league), sf: isSuperflex(league), teams, faab, budget, found: !!my };
  if (!my) return base;
  const claims = faab ? claimsFromTransactions(txByWeek || {}) : [];
  const needs = rosterNeeds({ rosterPositions: league.roster_positions || [], teams, myPlayers: my.players || [], players,
    targetWeek: board.target_week, weeklyOk, stashed: [...(my.reserve || []), ...(my.taxi || [])], injuries });
  return {
    ...base,
    myRosterId: my.roster_id,
    position: faab ? faabPosition(rosters, my.roster_id, budget) : null,
    needs,
    bids: faab ? bidHistory({ claims, budget, myRosterId: my.roster_id, players, seasonStartMs: board.season_start_ms }) : null,
    calib: faab ? calibrate({ claims, budget, players, history: board.bid_history, sf: base.sf, seasonStartMs: board.season_start_ms, model: board.model })
      : null,
    fy: forYou({ rosterPositions: league.roster_positions || [], teams, dynasty: base.type === 'dynasty', sf: base.sf,
      myPlayers: my.players || [], players, pickups: board.pickups.filter((p) => p.sid), needs, model: board.model }),
    avail: Object.fromEntries(board.pickups.filter((p) => p.sid).map((p) => [p.sid, availability(p.sid, rosters, my.roster_id)])),
  };
}
