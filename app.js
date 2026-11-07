import { isFaab, summarizeLeague, leagueBid, fmtBid, OFFENSE } from './logic.js?v=5';

const API = 'https://api.sleeper.app/v1';
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
};
const pct = (v) => (v === null || v === undefined || v === '' ? '' : `${Math.round(Number(v) * 10) / 10}%`);
const int = (v) => (v === null || v === undefined || v === '' ? '' : Math.round(Number(v)).toLocaleString());
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

let BOARD = null;
let PLAYERS = null;
let LEAGUES = [];        // summaries from logic.js
let SKIPPED = [];        // leagues left out, with the reason
const ui = { league: 'all', fmt: 'sf', pos: 'ALL', q: '', showTaken: false, sort: 'rank', dir: 1 };

async function getJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${r.status} on ${url}`);
  return r.json();
}

async function loadBoard() {
  [BOARD, PLAYERS] = await Promise.all([getJSON('data/board.json'), getJSON('data/players.json')]);
  PLAYERS = PLAYERS.players;
  $('week-line').textContent = `Week ${BOARD.source_week} waiver columns from ${BOARD.sources_used ?? '—'} of ${BOARD.sources_total} outlets. Updated ${BOARD.generated}.`;
  drawSources();
}

async function loadUser(username) {
  const status = $('status');
  status.textContent = 'Finding your leagues…';
  status.className = 'wb-status';
  $('board').hidden = true;
  const user = await getJSON(`${API}/user/${encodeURIComponent(username)}`);
  if (!user || !user.user_id) throw new Error(`No Sleeper user named “${username}”.`);
  const [state, leagues] = await Promise.all([
    getJSON(`${API}/state/nfl`),
    getJSON(`${API}/user/${user.user_id}/leagues/nfl/${BOARD.season}`),
  ]);
  const thruWeek = Math.max(1, Math.min(18, Number(state.leg || state.week) || BOARD.target_week));
  SKIPPED = [];
  const keep = [];
  for (const L of leagues || []) {
    if (L.status === 'pre_draft') SKIPPED.push({ name: L.name, why: 'not drafted yet' });
    else keep.push(L);
  }
  status.textContent = `Loading ${keep.length} league${keep.length === 1 ? '' : 's'}…`;
  LEAGUES = await Promise.all(keep.map(async (L) => {
    const weeks = isFaab(L) ? Array.from({ length: thruWeek }, (_, i) => i + 1) : [];
    const [rosters, ...tx] = await Promise.all([
      getJSON(`${API}/league/${L.league_id}/rosters`),
      ...weeks.map((w) => getJSON(`${API}/league/${L.league_id}/transactions/${w}`).catch(() => [])),
    ]);
    const txByWeek = Object.fromEntries(weeks.map((w, i) => [w, tx[i]]));
    return summarizeLeague({ league: L, rosters, txByWeek, userId: user.user_id, players: PLAYERS, board: BOARD });
  }));
  LEAGUES = LEAGUES.filter((l) => l.found);
  LEAGUES.forEach((l, i) => { l.key = `L${i}`; });
  const used = new Set();
  for (const l of LEAGUES) {
    let a = String(l.name).replace(/[^A-Za-z0-9]/g, '').slice(0, 3).toUpperCase() || 'LG';
    let n = 2;
    while (used.has(a)) a = a.slice(0, 2) + n++;
    used.add(a);
    l.abbr = a;
  }
  ui.league = 'all';
  $('who').textContent = user.display_name || username;
  if (!LEAGUES.length) {
    status.textContent = `${user.display_name || username} has no active NFL leagues on Sleeper this season.`
      + (SKIPPED.length ? ` Skipped: ${SKIPPED.map((s) => `${s.name} (${s.why})`).join(', ')}.` : '');
    return;
  }
  status.textContent = '';
  $('board').hidden = false;
  draw();
}

// ---------------------------------------------------------------- drawing
const picked = () => LEAGUES.find((l) => l.key === ui.league) || null;
// Superflex or 1-QB numbers: a league tab uses that league's format; "All leagues" uses the toggle (superflex by default).
const viewSf = () => { const L = picked(); return L ? L.sf : ui.fmt === 'sf'; };
const F = (sf) => (sf
  ? { rank: 'rank', bid: 'faab', mid: 'faab_mid', basis: 'faab_basis', dyn: 'dyn_value' }
  : { rank: 'rank_1qb', bid: 'faab_1qb', mid: 'faab_1qb_mid', basis: 'faab_1qb_basis', dyn: 'dyn_value_1qb' });

function drawChips() {
  const box = $('league-filter');
  const chips = [{ key: 'all', name: 'All leagues' }, ...LEAGUES];
  box.replaceChildren(...chips.map((l) => {
    const b = el('button', 'wb-chip', l.name);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(l.key === ui.league));
    b.addEventListener('click', () => { ui.league = l.key; draw(); });
    return b;
  }));
  $('skipped').textContent = SKIPPED.length ? `Not shown: ${SKIPPED.map((s) => `${s.name} (${s.why})`).join(', ')}.` : '';
}

// The bid to show: in a FAAB league tab, converted to that team's FAAB left; otherwise the general one.
function bid(r, L, sf) {
  const f = F(sf);
  const inputs = sf ? r.faab_in : r.faab_in_1qb;
  if (L && L.faab && L.position && inputs && inputs.length) {
    const b = leagueBid(inputs, L.position.myRemaining / L.position.budget, L.position.myRemainingPct);
    const why = `${r[f.basis] || 'Published FAAB'}. Scaled to your ${pct(L.position.myRemainingPct)} left`
      + (b.capped ? ', capped at what you have.' : '.');
    return { txt: fmtBid(b), mid: b.mid, title: why };
  }
  return { txt: r[f.bid], mid: r[f.mid], title: r[f.basis] };
}

function bestOpen() {
  const box = $('best');
  const lines = [];
  for (const l of LEAGUES) {
    const f = F(l.sf);
    const r = BOARD.pickups.filter((p) => l.avail[p.sid] === 'Open').sort((a, b) => a[f.rank] - b[f.rank])[0];
    if (r) lines.push([l, r, f]);
  }
  if (!lines.length) { box.textContent = 'None of the listed pickups are open in your leagues.'; return; }
  box.replaceChildren(el('span', null, LEAGUES.length > 1 ? 'Best pickup still open in each league:' : 'Best pickup still open: '));
  for (const [l, r, f] of lines) {
    const row = el('span', LEAGUES.length > 1 ? 'wb-best-row' : null);
    if (LEAGUES.length > 1) row.append(el('span', 'wb-sub', `${l.name}: `));
    const b = bid(r, l, l.sf).txt;
    row.append(el('b', null, r.player), el('span', 'wb-sub', ` ${r.pos}, ${r.team}${b ? ` · ${b}` : ''}`));
    box.append(row);
  }
}

function medianFor(l, pos) {
  const b = l && l.bids && l.bids.byPos[pos];
  return b ? b.median : null;
}

function columns() {
  const L = picked();
  const f = F(viewSf());
  const cols = [
    { f: 'rank', label: '#', num: true, k: 'c-rank', get: (r) => r[f.rank] },
    { f: 'player', label: 'Player', k: 'c-player' },
    { f: 'pos', label: 'Pos', k: 'c-sub' },
    { f: 'team', label: 'Team', k: 'c-sub wb-narrow-hide' },
    { f: 'bid', label: L && L.faab ? 'Your bid' : 'Bid', k: 'c-faab', get: (r) => bid(r, L, viewSf()).txt,
      sortGet: (r) => bid(r, L, viewSf()).mid, title: (r) => bid(r, L, viewSf()).title },
  ];
  if (L) {
    if (L.faab) cols.push({ f: 'med', label: 'League median', num: true, k: 'c-med', get: (r) => medianFor(L, r.pos), fmt: pct });
    if (L.type === 'dynasty') cols.push({ f: 'dyn', label: 'Dynasty', num: true, k: 'c-dyn', fmt: int, get: (r) => r[f.dyn] });
  } else {
    cols.push({ f: 'open', label: 'Leagues', k: 'c-lg', get: (r) => LEAGUES.filter((l) => l.avail[r.sid] === 'Open').length, pills: true });
  }
  return cols;
}

function drawPickups() {
  const L = picked();
  const cols = columns();
  const fr = F(viewSf()).rank;
  const val = (r, key) => { const c = cols.find((x) => x.f === key); if (!c) return r[key]; return c.sortGet ? c.sortGet(r) : c.get ? c.get(r) : r[key]; };
  let list = BOARD.pickups.slice();
  $('taken-toggle').hidden = !L;
  if (L && !ui.showTaken) list = list.filter((r) => L.avail[r.sid] === 'Open');
  // position chips with counts
  const counts = { ALL: list.length };
  for (const r of list) counts[r.pos] = (counts[r.pos] || 0) + 1;
  $('pos-filter').replaceChildren(...['ALL', ...OFFENSE].map((p) => {
    const b = el('button', null, p === 'ALL' ? 'All' : p);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(p === ui.pos));
    b.append(el('span', 'n', String(counts[p] || 0)));
    b.addEventListener('click', () => { ui.pos = p; drawPickups(); });
    return b;
  }));
  if (ui.pos !== 'ALL') list = list.filter((r) => r.pos === ui.pos);
  if (ui.q) list = list.filter((r) => `${r.player} ${r.team} ${r.pos}`.toLowerCase().includes(ui.q));
  list.sort((a, b) => {
    const x = val(a, ui.sort), y = val(b, ui.sort);
    if (x === y) return a[fr] - b[fr];
    if (x === null || x === undefined || x === '') return 1;
    if (y === null || y === undefined || y === '') return -1;
    const nx = Number(x), ny = Number(y);
    return (Number.isFinite(nx) && Number.isFinite(ny) ? nx - ny : String(x).localeCompare(String(y))) * ui.dir;
  });

  const table = $('pickups-table');
  const head = el('tr');
  for (const c of cols) {
    const key = c.f;
    const th = el('th', `${c.num ? 'num ' : ''}${c.k.includes('wb-narrow-hide') ? 'wb-narrow-hide' : ''}`);
    th.setAttribute('aria-sort', ui.sort === key ? (ui.dir > 0 ? 'ascending' : 'descending') : 'none');
    const btn = el('button', null, c.label);
    btn.type = 'button';
    btn.addEventListener('click', () => {
      if (ui.sort === key) ui.dir = -ui.dir; else { ui.sort = key; ui.dir = key === 'rank' || !(c.num || c.sortGet || c.pills) ? 1 : -1; }
      drawPickups();
    });
    th.append(btn);
    head.append(th);
  }
  table.tHead.replaceChildren(head);
  table.tBodies[0].replaceChildren(...list.map((r) => {
    const tr = el('tr');
    if (L) { const s = L.avail[r.sid]; tr.className = s === 'Taken' ? 'is-taken' : s === 'Yours' ? 'is-mine' : ''; }
    for (const c of cols) {
      const td = el('td', `${c.k}${c.num ? ' num' : ''}`);
      if (c.pills) {
        const wrap = el('span', 'wb-lg');
        for (const l of LEAGUES) {
          const s = l.avail[r.sid] || 'Open';
          const pill = el('span', s === 'Open' ? 'open' : s === 'Yours' ? 'mine' : 'taken', l.abbr);
          pill.title = `${l.name}: ${s}`;
          wrap.append(pill);
        }
        td.append(wrap);
      } else {
        const v = c.get ? c.get(r) : r[c.f];
        td.textContent = c.fmt ? c.fmt(v) : (v ?? '');
        if (c.title && c.title(r)) td.title = c.title(r);
      }
      tr.append(td);
    }
    return tr;
  }));
  $('pickups-empty').hidden = list.length > 0;

  const sf = viewSf();
  const tg = $('fmt-toggle');
  tg.hidden = !!L;
  for (const b of tg.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.fmt === ui.fmt));
  const who = L ? `${L.name} is ${sf ? 'superflex' : '1-QB'}. ` : '';
  const yourBid = L && L.faab && L.position ? ` Your bid is % of total budget, with outlets that quote % of what's left scaled to your ${pct(L.position.myRemainingPct)}.` : ' Bid is % of total budget.';
  $('pickups-note').textContent = sf
    ? `${who}Ranked by how many outlets list a player and how high.${yourBid} QB bids are superflex bids.`
    : `${who}1-QB rankings: QB bids are standard 1-QB bids, and QBs are ranked lower (they count about ${Math.round(100 * BOARD.qb_1qb_factor)}% as much as in superflex).${yourBid}`;
  const legend = $('pickups-legend');
  if (L) legend.textContent = ui.showTaken ? `All pickups for ${L.name}; rostered players are crossed out.` : `Open in ${L.name}, sorted by consensus.`;
  else {
    const keys = el('span', 'wb-lg');
    keys.append(el('span', 'open', 'Open'), el('span', 'mine', 'Yours'), el('span', 'taken', 'Taken'));
    legend.replaceChildren(keys, el('span', null, LEAGUES.map((l) => `${l.abbr} ${l.name}`).join(' · ')));
  }
}

function fill(id, rows, cells) {
  $(id).tBodies[0].replaceChildren(...rows.map((r) => {
    const tr = el('tr');
    for (const c of cells) {
      const td = el('td', c.num ? 'num' : '');
      c.draw(td, r);
      tr.append(td);
    }
    return tr;
  }));
}

function drawNeeds() {
  const weeklyOk = BOARD.weekly_ranks_week === BOARD.target_week;
  $('needs-note').textContent = `Starters ranked inside your league's starter range rest of season, minus Week ${BOARD.target_week} byes`
    + (weeklyOk ? ' and players missing from this week\'s rankings.' : '. (This week\'s rankings weren\'t available, so injuries aren\'t checked.)')
    + ' Need means a slot you can\'t fill; thin means no backup.';
  const rows = [];
  for (const l of LEAGUES) {
    if (ui.league !== 'all' && l.key !== ui.league) continue;
    for (const n of l.needs) rows.push({ league: l.name, ...n });
  }
  const LV = { need: 'Need', thin: 'Thin', ok: 'OK' };
  fill('needs-table', rows, [
    { draw: (td, r) => { td.textContent = r.league; } },
    { draw: (td, r) => { td.textContent = r.pos; } },
    { draw: (td, r) => { td.textContent = LV[r.level]; td.className = r.level === 'need' ? 'wb-flag-bad' : r.level === 'thin' ? 'wb-flag-warn' : 'wb-flag-ok'; } },
    { num: true, draw: (td, r) => { td.textContent = int(r.ready); } },
    { num: true, draw: (td, r) => { td.textContent = int(r.slots); } },
    { draw: (td, r) => {
      const d = [];
      if (r.onBye.length) d.push(`Week ${BOARD.target_week} bye: ${r.onBye.join(', ')}`);
      if (r.maybeOut.length) d.push(`Not ranked this week: ${r.maybeOut.join(', ')}`);
      td.textContent = d.join('; ');
    } },
  ]);
}

function drawBids() {
  const pos = [], top = [];
  for (const l of LEAGUES) {
    if (!l.bids || (ui.league !== 'all' && l.key !== ui.league)) continue;
    for (const [p, s] of Object.entries(l.bids.byPos)) pos.push({ league: l.name, pos: p, ...s });
    for (const c of l.bids.top) top.push({ league: l.name, ...c });
  }
  const max = Math.max(1, ...pos.map((r) => r.median));
  fill('bidpos-table', pos, [
    { draw: (td, r) => { td.textContent = r.league; } },
    { draw: (td, r) => { td.textContent = r.pos; } },
    { num: true, draw: (td, r) => { td.textContent = int(r.n); } },
    { draw: (td, r) => {
      const w = el('span', 'wb-bar'); const b = el('span', 'b');
      b.style.width = `${Math.max(2, (64 * r.median) / max)}px`;
      w.append(b, el('span', null, pct(r.median))); td.append(w);
    } },
    { num: true, draw: (td, r) => { td.textContent = pct(r.max); } },
  ]);
  $('bidpos-empty').hidden = pos.length > 0;
  fill('topbids-table', top, [
    { draw: (td, r) => { td.textContent = r.league; } },
    { draw: (td, r) => { td.textContent = r.player; } },
    { draw: (td, r) => { td.textContent = r.pos; } },
    { num: true, draw: (td, r) => { td.textContent = pct(r.pct); } },
    { num: true, draw: (td, r) => { td.textContent = int(r.week); } },
    { draw: (td, r) => { td.textContent = r.mine ? 'Yes' : ''; } },
  ]);
  $('topbids-empty').hidden = top.length > 0;
}

function drawSources() {
  fill('sources-table', BOARD.sources, [
    { draw: (td, r) => { if (r.url) { const a = el('a', null, r.source); a.href = r.url; a.rel = 'noopener'; a.target = '_blank'; td.append(a); } else td.textContent = r.source; } },
    { draw: (td, r) => { td.textContent = r.role; } },
    { draw: (td, r) => { td.textContent = r.status; td.className = r.status === 'Loaded' ? 'wb-flag-ok' : 'wb-flag-warn'; } },
    { num: true, draw: (td, r) => { td.textContent = r.players ?? ''; } },
  ]);
}

function draw() {
  drawChips();
  bestOpen();
  drawPickups();
  drawNeeds();
  drawBids();
}

// ---------------------------------------------------------------- wiring
$('fmt-toggle').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-fmt]');
  if (b) { ui.fmt = b.dataset.fmt; drawPickups(); }
});
$('search').addEventListener('input', (e) => { ui.q = e.target.value.trim().toLowerCase(); drawPickups(); });
$('show-taken').addEventListener('change', (e) => { ui.showTaken = e.target.checked; drawPickups(); });
$('user-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = $('username').value.trim();
  if (!name) return;
  store.set('sleeperUser', name);
  const u = new URL(location.href);
  u.searchParams.set('u', name);
  history.replaceState(null, '', u);
  try { await loadUser(name); } catch (err) { $('status').textContent = err.message.startsWith('No Sleeper') ? err.message : `Couldn't load from Sleeper: ${err.message}`; $('status').className = 'wb-status wb-flag-bad'; }
});

(async () => {
  try { await loadBoard(); } catch (err) { $('status').textContent = `Couldn't load this week's board: ${err.message}`; return; }
  const name = new URL(location.href).searchParams.get('u') || store.get('sleeperUser');
  if (name) { $('username').value = name; $('user-form').requestSubmit(); }
})();
