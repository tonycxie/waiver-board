// League math for the waiver board. Pure functions: no fetching, no DOM, so Node can test them.
// A port of the league half of build.py (claude/waiver/build.py in the Fantasy Football Waiver Manager project).

export const OFFENSE = ['QB', 'RB', 'WR', 'TE'];

// players.json rows: [name, pos, team, ros_pos_rank, weekly_pos_rank, bye, dyn_value, dyn_value_1qb]
export const P = { name: 0, pos: 1, team: 2, ros: 3, weekly: 4, bye: 5, dyn: 6, dyn1: 7 };

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
// scaled by what this team has left before the median. The result is capped at what's left.
const r1h = (x) => Math.floor(x * 10 + 0.5) / 10;  // halves up, same arithmetic as build.py's r1h
export function leagueBid(inputs, remFrac, capPct) {
  if (!inputs || !inputs.length) return null;
  const k = (v, rem) => (rem ? v * remFrac : v);
  let lo = median(inputs.map((x) => k(x[0], x[2])));
  let hi = median(inputs.map((x) => k(x[1], x[2])));
  let capped = false;
  if (capPct !== null && capPct !== undefined && hi > capPct) { lo = Math.min(lo, capPct); hi = capPct; capped = true; }
  return { lo: r1h(lo), hi: r1h(hi), mid: r1h((lo + hi) / 2), capped, n: inputs.length };
}

export const fmtBid = (b) => (!b ? null : b.lo === b.hi ? `${b.lo}%` : `${b.lo}–${b.hi}%`);

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
  return {
    ...base,
    myRosterId: my.roster_id,
    position: faab ? faabPosition(rosters, my.roster_id, budget) : null,
    needs: rosterNeeds({ rosterPositions: league.roster_positions || [], teams, myPlayers: my.players || [], players,
      targetWeek: board.target_week, weeklyOk, stashed: [...(my.reserve || []), ...(my.taxi || [])], injuries }),
    bids: faab ? bidHistory({ claims, budget, myRosterId: my.roster_id, players, seasonStartMs: board.season_start_ms }) : null,
    avail: Object.fromEntries(board.pickups.filter((p) => p.sid).map((p) => [p.sid, availability(p.sid, rosters, my.roster_id)])),
  };
}
