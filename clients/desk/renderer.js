// Trader Desk renderer. Every value goes in with textContent, never innerHTML, because
// decisions carry raw LLM output and research carries email text. The only markup set
// with innerHTML is the fixed icon table in icons.js.

const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5000;
const RESEARCH_MS = 60000;
const TICK_SECONDS = 60; // TRADER_LOOP_TICK_SECONDS default; the API does not expose it

const store = { decisions: [], audit: [], approvals: [], positions: [], orders: [], usage: null, risk: null, loop: null, view: null, notes: [], account: null, brokerPositions: [], history: null, brokerError: null };
const openKeys = new Set(); // expanded ledger rows and table rows survive refreshes
const seenKeys = new Set(); // rows already shown once; anything new gets the arrival motion
let firstLedger = true;
let apiUp = false;
let paused = null;

// ---------- small helpers ----------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k === 'icon') node.innerHTML = ICO[v] || '';
    else node[k] = v;
  }
  for (const c of children) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}
const icon = (name, cls = 'ico') => el('span', { className: cls, icon: name });
const mono = (text) => el('span', { className: 'mono', textContent: text ?? '—' });
const usd = (v, digits = 2) => (v == null ? '—' : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`);
const num = (v, digits = 4) => (v == null ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits }));
const pct = (v) => (v == null ? '—' : `${Math.round(Number(v) * 100)}%`);
const secs = (ms) => (ms == null ? '—' : `${(ms / 1000).toFixed(1)}s`);
const words = (s) => String(s || '').replaceAll('_', ' ');
const abs = (iso) => (iso ? new Date(iso).toLocaleString() : '—');
const dayKey = (iso) => new Date(iso).toDateString();
const isToday = (iso) => iso && dayKey(iso) === new Date().toDateString();

function rel(iso) {
  if (!iso) return '—';
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return 'just now';
  if (s < 90) return '1m ago';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
// Relative times re-render every second without rebuilding their rows.
const when = (iso) => el('span', { className: 'num', textContent: rel(iso), title: abs(iso), dataset: { ts: iso || '' } });

function dayLabel(iso) {
  const d = new Date(iso);
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === new Date().toDateString()) return 'Today';
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

function toast(text, bad = false) {
  const t = el('div', { className: `toast${bad ? ' bad' : ''}`, textContent: text });
  $('toasts').append(t);
  setTimeout(() => t.remove(), 5000);
}

function ask(title, body, okLabel) {
  const dlg = $('confirm');
  $('confirm-title').textContent = title;
  $('confirm-body').textContent = body;
  $('confirm-ok').textContent = okLabel;
  dlg.showModal();
  return new Promise((resolve) => {
    const done = (v) => {
      dlg.close();
      $('confirm-ok').onclick = $('confirm-cancel').onclick = dlg.oncancel = null;
      resolve(v);
    };
    $('confirm-ok').onclick = () => done(true);
    $('confirm-cancel').onclick = () => done(false);
    dlg.oncancel = () => done(false);
  });
}

function empty(title, hint) {
  return el('div', { className: 'empty' }, el('b', { textContent: title }), hint || '');
}

// r: right-aligned numbers · minor: hidden on narrow windows · wrap: long text
const cls = (c, cell) => [c.r ? (cell === 'td' ? 'r num' : 'r') : '', c.minor ? 'minor' : '', c.wrap && cell === 'td' ? 'wrap' : ''].filter(Boolean).join(' ');

function table(columns, rows, { key, detail } = {}) {
  if (!rows.length) return null;
  const body = el('tbody');
  for (const row of rows) {
    const k = key?.(row);
    const open = k && openKeys.has(k);
    const tr = el('tr', { className: `${detail ? 'click' : ''}${open ? ' open' : ''}` }, ...columns.map((c) => {
      const v = c.get(row);
      return el('td', { className: cls(c, 'td') }, v instanceof Node ? v : String(v ?? '—'));
    }));
    if (detail) {
      tr.onclick = () => {
        openKeys.has(k) ? openKeys.delete(k) : openKeys.add(k);
        renderAll();
      };
    }
    body.append(tr);
    if (open) body.append(el('tr', { className: 'detailrow' }, el('td', { colSpan: columns.length }, detail(row))));
  }
  return el('table', {}, el('thead', {}, el('tr', {}, ...columns.map((c) => el('th', { className: cls(c, 'th'), textContent: c.h })))), body);
}

// ---------- reading a decision in plain words ----------
// Jev writes its judgments into the rationale ("Jev judged hold at 98% (open 2%, ...).
// Edge: weak at 70% ..."). These pull out what a person wants on a hold: how close it
// came to buying, and how strong the setup looked. Null for other models' rationales.
function jevRead(d) {
  const r = d.decision?.rationale || '';
  if (!r.startsWith('Jev judged')) return null;
  const pctOf = (re) => { const m = r.match(re); return m ? Number(m[1]) : null; };
  const edge = r.match(/Edge: (none|weak|moderate|strong) at/);
  return { open: pctOf(/open (\d+)%/), close: pctOf(/close (\d+)%/), edge: edge ? edge[1] : null };
}

function describeDecision(d) {
  const dec = d.decision || {};
  const sym = d.symbol || 'the market';
  const size = dec.notional_usd ? `${usd(dec.notional_usd, 0)} of ` : '';
  if (d.status === 'error' || !d.decision) {
    return { tone: 'bad', ico: 'alert', verb: 'Decision failed', rest: [el('span', { className: 'lead', textContent: ' on ' }), mono(sym)], status: 'Error' };
  }
  // An open journaled as shadow while the loop is live came from TRADER_SHADOW_SYMBOLS.
  const shadow = d.status === 'shadow';
  const statusWord = { shadow: 'Shadow', executed: 'Executed', denied: 'Denied', proposed: 'Proposed' }[d.status] || words(d.status);
  if (dec.action === 'open') {
    const buy = dec.side !== 'sell';
    const verb = shadow ? (buy ? 'Would buy' : 'Would sell') : d.status === 'denied' ? (buy ? 'Buy denied' : 'Sell denied') : buy ? 'Bought' : 'Sold';
    // A shadow call is still volatile (a proposal); a fill is fixed.
    return { tone: d.status === 'denied' ? '' : shadow ? 'live' : 'win', ico: buy ? 'up' : 'down', verb, rest: [` ${size}`, mono(sym)], status: statusWord };
  }
  if (dec.action === 'close') {
    return { tone: d.status === 'denied' ? '' : shadow ? 'live' : 'win', ico: 'down', verb: shadow ? 'Would close' : d.status === 'denied' ? 'Close denied' : 'Closed', rest: [' ', mono(sym)], status: statusWord };
  }
  // Trader journals every hold as "shadow" in any mode (a hold never reaches the guard),
  // so the desk says what actually happened.
  return {
    tone: '',
    ico: 'hold',
    verb: 'Held',
    rest: [' ', mono(sym), el('span', { className: 'lead', textContent: holdLead(d) })],
    status: 'No order',
  };
}

function holdLead(d) {
  const j = jevRead(d);
  if (j && j.open != null) return ` · buy chance ${j.open}%${j.edge ? ` · edge ${j.edge}` : ''}`;
  return ` · confidence ${pct(d.decision?.conviction)}`;
}

function decisionDetail(d) {
  const dec = d.decision || {};
  const parts = [];
  if (dec.rationale) parts.push(el('div', { className: 'k', textContent: 'Why' }), el('p', { textContent: dec.rationale }));
  const verdicts = Array.isArray(d.risk_verdicts) ? d.risk_verdicts : [];
  if (verdicts.length) {
    parts.push(
      el('div', { className: 'k', textContent: 'RiskGuard' }),
      el('div', { className: 'verdicts' }, ...verdicts.map((v) => {
        const tone = v.action === 'deny' ? 'bad' : v.action === 'clamp' ? 'warn' : 'ok';
        return el('span', { className: `pill ${tone}`, title: v.reason || '', textContent: `${words(v.check)} · ${v.action}` });
      })),
    );
  }
  if (!dec.rationale && d.raw_output) parts.push(el('div', { className: 'k', textContent: 'Model output' }), el('pre', { textContent: d.raw_output }));
  const meta = [`Trigger: ${words(d.trigger)}`, `decided in ${secs(d.latency_ms)}`];
  if (d.cost_usd != null) meta.push(`cost ${usd(d.cost_usd, 4)}`);
  if (d.macro_view_version) meta.push(`Macro View v${d.macro_view_version}`);
  parts.push(el('div', { className: 'sub', textContent: meta.join(' · ') }));
  return el('div', { className: 'detail' }, ...parts);
}

// ---------- reading an audit event in plain words ----------
function describeAudit(a) {
  const p = a.payload || {};
  const lead = (text) => el('span', { className: 'lead', textContent: text });
  switch (a.action) {
    case 'compile_view':
      return { ico: 'spark', verb: 'Macro View compiled', rest: [lead(` from ${(p.note_ids || []).length} notes${p.compiled_by ? ` by ${p.compiled_by.split(':').pop()}` : ''}`)] };
    case 'submit_note':
      return { ico: 'note', verb: 'Research note added', rest: [lead(p.title ? ` · ${p.title}` : '')] };
    case 'retire_note':
      return { ico: 'note', verb: 'Research note retired', rest: [] };
    case 'wake_trader':
      return { ico: 'pulse', verb: 'Loop woken', rest: [lead(' by you')] };
    case 'pause':
      return { ico: 'pause', tone: 'warn', verb: 'Trading paused', rest: [lead(p.reason ? ` · ${p.reason}` : '')] };
    case 'resume':
      return { ico: 'play', verb: 'Trading resumed', rest: [] };
    case 'resolve_approval': {
      const line = resolveAuditLine(p);
      return { ico: 'shield', verb: line.verb, rest: [lead(`${line.extra}${a.subject ? ` · ${a.subject}` : ''}`)] };
    }
    default:
      return { ico: a.category === 'order' ? 'order' : 'gear', verb: words(a.action).replace(/^./, (c) => c.toUpperCase()), rest: [lead(` · ${words(a.category)}`)] };
  }
}

// ---------- the ledger (activity rows, grouped by day) ----------
function ledgerItems() {
  const items = [
    ...store.decisions.map((d) => ({ key: `d${d.id}`, ts: d.ts, kind: 'decision', row: d })),
    ...store.audit.map((a) => ({ key: `a${a.id}`, ts: a.ts, kind: 'audit', row: a })),
  ];
  return items.sort((x, y) => new Date(y.ts) - new Date(x.ts)).slice(0, 80);
}

function renderLedger() {
  const items = ledgerItems();
  if (!items.length) {
    $('ledger').replaceChildren(empty('Nothing has happened yet', 'Decisions, research and your own actions show up here as they happen.'));
    return;
  }
  const out = [];
  let currentDay = null;
  let list = null;
  for (const it of items) {
    const day = dayKey(it.ts);
    if (day !== currentDay) {
      currentDay = day;
      list = el('ul', { className: 'rows' });
      out.push(el('div', { className: 'boundary', textContent: dayLabel(it.ts) }), list);
    }
    const info = it.kind === 'decision' ? describeDecision(it.row) : describeAudit(it.row);
    const open = openKeys.has(it.key);
    const meta = el('span', { className: 'rm' });
    if (info.status) meta.append(el('span', { className: 'st', textContent: info.status }));
    if (it.kind === 'decision' && it.row.latency_ms && info.tone !== 'bad') meta.append(el('span', { textContent: secs(it.row.latency_ms) }));
    meta.append(when(it.ts));
    const btn = el('button', { className: 'rowbtn' },
      el('span', { className: 'ri', icon: info.ico }),
      el('span', { className: 'rl' }, el('b', { textContent: info.verb }), ...info.rest),
      meta,
      el('span', { className: 'chev', icon: 'chevron' }),
    );
    btn.setAttribute('aria-expanded', String(open));
    btn.onclick = () => {
      openKeys.has(it.key) ? openKeys.delete(it.key) : openKeys.add(it.key);
      renderLedger();
    };
    const li = el('li', { className: `row${!firstLedger && !seenKeys.has(it.key) ? ' fresh' : ''}`, dataset: { tone: info.tone || '' } }, btn);
    if (open) {
      li.append(it.kind === 'decision'
        ? decisionDetail(it.row)
        : el('div', { className: 'detail' },
            el('div', { className: 'sub', textContent: `${words(it.row.category)} · by ${it.row.actor}${it.row.subject ? ` · ${it.row.subject}` : ''}` }),
            el('pre', { textContent: JSON.stringify(it.row.payload, null, 2) })));
    }
    seenKeys.add(it.key);
    list.append(li);
  }
  firstLedger = false;
  $('ledger').replaceChildren(...out);
}

// ---------- live line + tiles ----------
function watchedSymbols() {
  const syms = [...new Set(store.decisions.slice(0, 20).map((d) => d.symbol).filter(Boolean))].sort();
  return syms.length ? syms.join(' and ') : 'the market';
}

function renderLiveLine() {
  const loop = store.loop;
  const line = $('liveline');
  const parts = [];
  let tone = '';
  if (!apiUp) {
    parts.push(el('span', { className: 'what', textContent: 'Waiting for the trader API' }));
  } else if (!loop?.running) {
    parts.push(el('span', { className: 'what', textContent: 'The loop is off' }), el('span', { className: 'then', textContent: 'Set TRADER_LOOP_ENABLED=true in trader’s .env to let the agent decide' }));
  } else {
    const s = loop.stats || {};
    tone = s.last_error ? 'warn' : 'on';
    const next = s.last_tick_at ? Math.round((new Date(s.last_tick_at).getTime() + TICK_SECONDS * 1000 - Date.now()) / 1000) : null;
    parts.push(el('span', { className: 'what', textContent: `Watching ${watchedSymbols()}` }));
    parts.push(el('span', { className: 'then', textContent: s.last_error ? `last attempt failed: ${s.last_error}` : next == null ? 'first check starting' : next > 0 ? `next check in ${next}s` : 'checking now' }));
    parts.push(el('span', { className: 'sub num stats', textContent: `${s.ticks ?? 0} checks · ${s.decisions ?? 0} decisions · ${s.errors ?? 0} errors since start` }));
  }
  line.className = `liveline${tone === 'on' ? ' running' : ''}`;
  line.replaceChildren(el('span', { className: `pulse ${tone}` }), ...parts);

  $('footLoop').replaceChildren(
    el('span', { className: `pulse ${tone}` }),
    el('span', { className: 'txt', textContent: loop?.running ? (tone === 'warn' ? 'Loop running, with errors' : 'Loop running') : 'Loop off' }),
  );
}

function todayStats() {
  const today = store.decisions.filter((d) => isToday(d.ts));
  const good = today.filter((d) => d.status !== 'error' && d.decision);
  const errors = today.filter((d) => d.status === 'error' || !d.decision);
  const held = good.filter((d) => d.decision.action === 'hold').length;
  // Recent calls only, so the figure describes the model running now, not the whole day.
  const lat = store.decisions.filter((d) => d.status !== 'error' && d.decision).slice(0, 20).map((d) => d.latency_ms).filter(Boolean);
  const avg = lat.length ? lat.reduce((a, b) => a + b, 0) / lat.length : null;
  const utcToday = new Date().toISOString().slice(0, 10);
  const rows = store.usage?.rows || [];
  const usageToday = rows.filter((r) => r.day === utcToday);
  const cost = usageToday.reduce((a, r) => a + Number(r.cost_usd || 0), 0);
  const requests = usageToday.reduce((a, r) => a + Number(r.requests || 0), 0);
  const decideModel = rows.find((r) => r.role === 'decide');
  return { today, good, errors, held, acted: good.length - held, avg, cost, requests, lastError: errors[0], decideModel };
}

function tile(value, label, sub, tone = '') {
  return el('div', { className: `tile ${tone}` }, el('div', { className: 'v', textContent: value }), el('div', { className: 'l', textContent: label }), sub ? el('div', { className: 's', textContent: sub }) : null);
}

function renderTiles() {
  const t = todayStats();
  const pending = store.approvals.filter((a) => a.status === 'pending').length;
  $('tiles').replaceChildren(
    tile(String(t.today.length), 'Decisions today', `${t.held} held · ${t.acted} acted`, store.loop?.running ? 'live' : ''),
    tile(String(t.errors.length), 'Errors today', t.lastError ? `last ${rel(t.lastError.ts)}` : 'None', t.errors.length ? 'bad' : ''),
    tile(t.avg ? secs(t.avg) : '—', 'Decision time, last 20', t.decideModel ? t.decideModel.model : ''),
    tile(usd(t.cost), 'Model cost today', `${t.requests} requests`),
    tile(String(store.positions.length), 'Open positions', `${store.orders.filter((o) => isToday(o.created_at)).length} orders today`),
    tile(String(pending), 'Waiting for you', pending ? 'Approvals in Book' : 'Nothing to approve', pending ? 'warn' : ''),
  );
  const fresh = t.good[0] && Date.now() - new Date(t.good[0].ts).getTime() < 300000;
  $('footModel').replaceChildren(
    el('span', { className: `pulse ${fresh ? 'on' : ''}` }),
    el('span', { className: 'txt sub', textContent: t.decideModel ? `${t.decideModel.model} · ${t.avg ? secs(t.avg) : '—'}` : 'No decide calls yet' }),
  );
}

// ---------- header, shift bar, briefing ----------
function renderHeader() {
  const r = store.risk;
  if (!r) return;
  paused = r.trading_paused;
  const btn = $('pause-toggle');
  btn.disabled = false;
  btn.textContent = paused ? 'Resume trading' : 'Pause trading';
  btn.className = paused ? 'primary' : 'danger';
  $('mode').textContent = r.live_enabled ? 'LIVE MONEY' : 'PAPER';
  $('mode').className = `pill ${r.live_enabled ? 'bad' : ''}`;

  const bar = $('shiftBar');
  if (r.trading_paused || r.halted_reason || r.drift_detected) {
    bar.className = 'gating';
    bar.replaceChildren(
      el('b', { textContent: r.trading_paused ? 'Trading is paused' : r.halted_reason ? 'A breaker halted trading' : 'Position drift detected' }),
      el('span', { textContent: r.blocked_reason || 'RiskGuard denies every order until this clears.' }),
    );
  } else if (store.loop?.shadow) {
    bar.className = '';
    bar.replaceChildren(el('b', { textContent: 'Shadow mode' }), el('span', { textContent: 'The agent decides and journals every call, but places no orders.' }));
  } else {
    bar.className = 'hidden';
  }
}

let briefText = '';
function renderBrief() {
  const bar = $('briefBar');
  const today = new Date().toDateString();
  let dismissed = null;
  try { dismissed = localStorage.getItem('trader-desk:brief'); } catch { /* storage may be unavailable */ }
  if (dismissed === today || !apiUp) return bar.classList.add('hidden');

  const t = todayStats();
  const items = [];
  const last = store.decisions[0];
  if (last) items.push({ parts: [['Last call: '], [describeDecision(last).verb, true], [` ${last.symbol || ''} · ${rel(last.ts)}`]] });
  items.push({ parts: [[String(t.today.length), true], [' decisions today, '], [String(t.errors.length), true], [' errors']], warn: t.errors.length > 0 });
  if (t.decideModel && t.avg) items.push({ parts: [['Decide model '], [t.decideModel.model, true], [' answers in '], [secs(t.avg), true]] });
  if (store.view?.global_thesis) items.push({ parts: [['Macro View: '], [store.view.global_thesis.replace(/\s*\[note:\d+\]/g, '').split(/(?<=\.)\s/)[0]]] });
  const pending = store.approvals.filter((a) => a.status === 'pending').length;
  if (pending) items.push({ parts: [[String(pending), true], [' approvals waiting for you']], warn: true });
  if (store.risk?.trading_paused) items.push({ parts: [['Trading is paused']], warn: true });

  // Rebuilding restarts the scroll, so only rebuild when the words change (minutes, not seconds).
  const text = JSON.stringify(items.map((i) => i.parts.map((p) => p[0]).join('').replace(/just now|\d+[mh] ago/, '')));
  if (text === briefText && !bar.classList.contains('hidden')) return;
  briefText = text;
  const seg = el('span', { className: 'seg' });
  for (const item of items) seg.append(el('span', { className: `item${item.warn ? ' warn' : ''}` }, ...item.parts.map(([s, bold]) => (bold ? el('b', { textContent: s }) : s))));
  const run = el('div', { className: 'run' }, seg);
  const track = el('div', { className: 'track' }, run);
  const x = el('button', { className: 'x', title: 'Hide until tomorrow', textContent: '✕' });
  x.onclick = () => {
    try { localStorage.setItem('trader-desk:brief', today); } catch { /* ignore */ }
    bar.classList.add('hidden');
  };
  bar.replaceChildren(track, x);
  bar.classList.remove('hidden');
  const inW = track.clientWidth;
  const outW = run.scrollWidth;
  run.style.setProperty('--brief-in', `${inW}px`);
  run.style.setProperty('--brief-out', `${outW}px`);
  run.style.setProperty('--brief-secs', `${Math.max(20, Math.round((inW + outW) / 52))}s`);
}

// ---------- the account (read-only, straight from Alpaca) ----------
const BROKER_MS = 30000;
const signedUsd = (v) => `${v < 0 ? '−' : v > 0 ? '+' : ''}${usd(Math.abs(v))}`;

function sparkline(values) {
  const w = 300, h = 70, pad = 4;
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = hi - lo || 1;
  const pts = values.map((v, i) => [pad + (i * (w - 2 * pad)) / Math.max(values.length - 1, 1), hi === lo ? h / 2 : h - pad - ((v - lo) / span) * (h - 2 * pad)]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'spark');
  const area = document.createElementNS(ns, 'polygon');
  area.setAttribute('points', `${pad},${h} ${line} ${w - pad},${h}`);
  area.setAttribute('fill', 'rgba(245,184,46,0.10)');
  const poly = document.createElementNS(ns, 'polyline');
  poly.setAttribute('points', line);
  poly.setAttribute('fill', 'none');
  poly.setAttribute('stroke', '#f5b82e');
  poly.setAttribute('stroke-width', '2');
  poly.setAttribute('vector-effect', 'non-scaling-stroke');
  const [ex, ey] = pts[pts.length - 1];
  const dot = document.createElementNS(ns, 'circle');
  dot.setAttribute('cx', ex); dot.setAttribute('cy', ey); dot.setAttribute('r', '3.5'); dot.setAttribute('fill', '#f5b82e');
  svg.append(area, poly, dot);
  return svg;
}

function renderAccount() {
  const box = $('account');
  if (store.brokerError && !store.account) {
    return box.replaceChildren(el('span', { className: 'error', textContent: `Account unavailable: ${store.brokerError}` }));
  }
  const a = store.account;
  if (!a) return;
  const equity = Number(a.equity), last = Number(a.last_equity);
  const change = equity - last;
  const pctChange = last ? (change / last) * 100 : 0;
  const series = (store.history?.equity || []).filter((v) => v != null && v > 0);
  const flat = series.length > 1 && Math.max(...series) === Math.min(...series);
  box.replaceChildren(
    el('div', {},
      el('div', { className: 'label', textContent: 'Alpaca paper account' }),
      el('div', { className: 'eq', textContent: usd(equity) }),
      el('div', { className: `chg ${change > 0 ? 'up' : change < 0 ? 'down' : 'flat'}`, textContent: change ? `${signedUsd(change)} (${pctChange >= 0 ? '+' : '−'}${Math.abs(pctChange).toFixed(2)}%) today` : 'No change today' }),
      el('div', { className: 'stats' },
        el('span', {}, 'Cash ', el('b', { textContent: usd(Number(a.cash)) })),
        el('span', {}, 'Buying power ', el('b', { textContent: usd(Number(a.buying_power)) })),
        el('span', {}, 'Holdings ', el('b', { textContent: String(store.brokerPositions.length) })),
      ),
    ),
    series.length > 1
      ? el('div', {}, sparkline(series), el('div', { className: 'sparkcap', textContent: flat ? '30 days · flat: no trades yet' : `30 days · ${usd(series[0])} → ${usd(series[series.length - 1])}` }))
      : el('div', { className: 'sparkcap', textContent: 'No balance history yet' }),
  );
}

function renderBrokerPositions() {
  const rows = store.brokerPositions;
  $('broker-positions').replaceChildren(
    table([
      { h: 'Symbol', get: (p) => mono(p.symbol) },
      { h: 'Quantity', get: (p) => num(Number(p.qty)), r: true },
      { h: 'Average entry', get: (p) => usd(Number(p.avg_entry_price)), r: true },
      { h: 'Market value', get: (p) => usd(Number(p.market_value)), r: true },
      {
        h: 'Unrealized P&L',
        get: (p) => {
          const v = Number(p.unrealized_pl);
          return el('span', { className: v > 0 ? 'pnl-up' : 'pnl-down', textContent: `${signedUsd(v)} (${(Number(p.unrealized_plpc) * 100).toFixed(2)}%)` });
        },
        r: true,
      },
    ], rows) || empty(store.brokerError ? `Unavailable: ${store.brokerError}` : 'Nothing held at Alpaca', 'Cash only. The agent has not bought anything yet: shadow mode records its calls without sending orders.'),
  );
}

async function refreshBroker() {
  try {
    const [account, positions, history] = await Promise.all([window.trader.broker('account'), window.trader.broker('positions'), window.trader.broker('history')]);
    Object.assign(store, { account, brokerPositions: positions, history, brokerError: null });
  } catch (err) {
    store.brokerError = err.message;
  }
  renderAccount();
  renderBrokerPositions();
}

// ---------- book ----------
function intentText(i) {
  const size = i.qty != null ? `${num(i.qty)} ` : i.notional != null ? `${usd(i.notional, 0)} of ` : '';
  return `${(i.side || '?').toUpperCase()} ${size}${i.symbol || '?'}`;
}

function renderBook() {
  const pending = store.approvals.filter((a) => a.status === 'pending');
  $('approvals').replaceChildren(
    ...(pending.length
      ? pending.map((a) => {
          const approve = el('button', { className: 'primary', textContent: 'Approve' });
          const deny = el('button', { className: 'danger', textContent: 'Deny' });
          approve.onclick = () => resolveApproval(a, true);
          deny.onclick = () => resolveApproval(a, false);
          return el('div', { className: 'approval' },
            el('div', { className: 'what' }, el('b', { textContent: intentText(a.order_intent || {}) }), el('div', { className: 'sub', textContent: `#${a.id} · ${a.reason} · ${abs(a.created_at)}` })),
            approve, deny);
        })
      : [empty('Nothing waiting', 'With a park band set (park_above_usd), RiskGuard parks large opens here for a human yes or no.')]),
  );
  $('positions').replaceChildren(
    table([
      { h: 'Symbol', get: (p) => mono(p.symbol) },
      { h: 'Class', get: (p) => p.asset_class, minor: true },
      { h: 'Quantity', get: (p) => num(p.qty), r: true },
      { h: 'Average entry', get: (p) => num(p.avg_entry_price), r: true },
      { h: 'Updated', get: (p) => when(p.updated_at) },
    ], store.positions) || empty('No open positions', 'In shadow mode the agent decides but never opens one.'),
  );
  $('orders').replaceChildren(
    table([
      { h: 'Time', get: (o) => when(o.created_at) },
      { h: 'Symbol', get: (o) => mono(o.symbol) },
      { h: 'Side', get: (o) => o.side },
      { h: 'Size', get: (o) => (o.qty != null ? num(o.qty) : usd(o.notional)), r: true },
      { h: 'Type', get: (o) => o.order_type, minor: true },
      { h: 'Status', get: (o) => el('span', { className: `pill ${o.status === 'filled' ? 'ok' : o.status === 'rejected' || o.status === 'error' ? 'bad' : ''}`, textContent: o.status }) },
    ], store.orders) || empty('No orders yet'),
  );
}

async function resolveApproval(a, approved) {
  const verb = approved ? 'Approve' : 'Deny';
  if (!(await ask(`${verb} this order?`, `#${a.id}: ${intentText(a.order_intent || {})}. ${a.reason}`, verb))) return;
  try {
    const body = await window.trader.post(`/risk/approvals/${a.id}/resolve?approved=${approved}&resolved_by=trader-desk`);
    const outcome = approvalToast(body, approved, a.id);
    toast(outcome.text, !outcome.ok);
  } catch (err) {
    toast(`${verb} failed: ${err.message}`, true);
  }
  refresh();
}

// ---------- decisions ----------
function renderDecisions() {
  $('decisions').replaceChildren(
    table([
      { h: 'Time', get: (d) => when(d.ts) },
      { h: 'Symbol', get: (d) => mono(d.symbol) },
      {
        h: 'Call',
        get: (d) => {
          const info = describeDecision(d);
          const tone = { bad: 'bad', win: 'ok', warn: 'warn', live: 'live' }[info.tone] || '';
          return el('span', { className: `pill ${tone}`, textContent: info.verb });
        },
      },
      { h: 'Buy chance', get: (d) => { const j = jevRead(d); return j && j.open != null ? `${j.open}%` : '—'; }, r: true },
      { h: 'Confidence', get: (d) => pct(d.decision?.conviction), r: true, minor: true },
      { h: 'Size', get: (d) => (d.decision?.notional_usd ? usd(d.decision.notional_usd, 0) : '—'), r: true, minor: true },
      { h: 'Trigger', get: (d) => words(d.trigger), minor: true },
      { h: 'Status', get: (d) => words(d.status), minor: true },
      { h: 'Time to decide', get: (d) => secs(d.latency_ms), r: true },
    ], store.decisions, { key: (d) => `t${d.id}`, detail: decisionDetail }) || empty('No decisions yet', 'Turn the loop on and the agent’s calls land here.'),
  );
}

// ---------- research ----------
function withCitations(s) {
  const out = el('span');
  for (const piece of String(s).split(/(\[note:\d+\])/)) out.append(/^\[note:\d+\]$/.test(piece) ? el('span', { className: 'cite', textContent: piece }) : piece);
  return out;
}

function renderResearch() {
  const v = store.view;
  $('macro-view').replaceChildren(
    ...(v
      ? [
          el('div', { className: 'sub', textContent: `Version ${v.version} · ${v.compiled_by} · ${abs(v.created_at)} · from notes ${v.note_ids.join(', ')}` }),
          el('p', { className: 'thesis' }, withCitations(v.global_thesis)),
          ...Object.entries(v.sections || {}).map(([name, text]) => el('div', { className: 'section' }, el('h3', { textContent: words(name) }), el('p', {}, withCitations(text)))),
        ]
      : [empty('Not compiled yet', 'It needs an xAI login (SuperGrok OAuth or an API key) and at least one active note.')]),
  );
  $('notes').replaceChildren(
    table([
      { h: '#', get: (n) => n.id },
      { h: 'Title', get: (n) => n.title, wrap: true },
      { h: 'Written', get: (n) => new Date(n.authored_at || n.created_at).toLocaleDateString(), minor: true },
      { h: 'Expires', get: (n) => (n.pinned ? 'Pinned' : new Date(n.expires_at).toLocaleDateString()) },
      { h: 'Status', get: (n) => el('span', { className: `pill ${n.status === 'active' ? 'ok' : ''}`, textContent: n.status }) },
    ], store.notes, {
      key: (n) => `n${n.id}`,
      detail: (n) => el('div', { className: 'detail' }, el('div', { className: 'sub', textContent: n.source || '' }), el('pre', { textContent: n.body })),
    }) || empty('No research notes', 'Notes added through the API, MCP or CLI land here.'),
  );
}

// ---------- risk ----------
function kv(target, pairs) {
  target.replaceChildren(...pairs.flatMap(([k, v]) => [el('dt', { textContent: k }), el('dd', { textContent: v ?? '—' })]));
}

function renderRisk() {
  const r = store.risk;
  if (!r) return;
  kv($('risk'), [
    ['Trading', r.trading_paused ? `Paused (${r.pause_reason || 'operator'})` : 'Active'],
    ['Halted', r.halted_reason || 'No'],
    ['Drift', r.drift_detected ? 'Detected' : 'None'],
    ['Day start equity', usd(r.day_start_equity)],
    ['Week high equity', usd(r.week_high_equity)],
    ['Routing', r.live_enabled ? 'Live money enabled' : 'Paper only'],
  ]);
  kv($('limits'), Object.entries(r.limits || {}).map(([k, v]) => [words(k), String(v)]));
  $('usage').replaceChildren(
    table([
      { h: 'Day', get: (u) => u.day },
      { h: 'Role', get: (u) => u.role },
      { h: 'Model', get: (u) => mono(u.model) },
      { h: 'Calls', get: (u) => u.requests, r: true },
      { h: 'Cost', get: (u) => usd(u.cost_usd, 3), r: true },
    ], store.usage?.rows || []) || empty('No model calls yet'),
  );
}

// ---------- nav (rail) ----------
const NAV = [
  { id: 'live', label: 'Live', ico: 'pulse' },
  { id: 'book', label: 'Book', ico: 'chart' },
  { id: 'decisions', label: 'Decisions', ico: 'scale' },
  { id: 'research', label: 'Research', ico: 'book' },
  { id: 'risk', label: 'Risk', ico: 'shield' },
];

function currentView() {
  const id = location.hash.replace(/^#\/?/, '');
  return NAV.some((n) => n.id === id) ? id : 'live';
}

let shownView = null;
function renderNav() {
  const active = currentView();
  const t = todayStats();
  const pending = store.approvals.filter((a) => a.status === 'pending').length;
  const counts = {
    book: pending ? [String(pending), 'alert'] : null,
    decisions: t.today.length ? [String(t.today.length), ''] : null,
    research: store.notes.length ? [String(store.notes.length), ''] : null,
  };
  $('nav').replaceChildren(...NAV.map((n) => {
    const ic = icon(n.ico);
    if (n.id === 'live' && store.loop?.running) ic.append(el('span', { className: 'dot' }));
    const b = el('button', { className: `navitem${n.id === active ? ' active' : ''}`, title: n.label },
      ic, el('span', { className: 'lbl', textContent: n.label }),
      counts[n.id] ? el('span', { className: `count ${counts[n.id][1]}`, textContent: counts[n.id][0] }) : null);
    b.onclick = () => {
      if (n.id === active && !document.body.classList.contains('navopen')) setRail(true);
      location.hash = `#/${n.id}`;
    };
    return b;
  }));
  // Only toggle on a real change so the entrance motion plays once per visit.
  if (active !== shownView) {
    shownView = active;
    for (const v of document.querySelectorAll('.view')) v.classList.toggle('active', v.id === `view-${active}`);
  }
}

function setRail(open) {
  document.body.classList.toggle('navopen', open);
  if (!NARROW()) {
    try { localStorage.setItem('trader-desk:nav', open ? 'open' : 'rail'); } catch { /* ignore */ }
  }
  const t = $('navToggle');
  t.querySelector('.ico').innerHTML = ICO[open ? 'left' : 'right'];
  t.title = open ? 'Collapse the menu' : 'Expand the menu';
}

// ---------- data ----------
async function load(route, key, fallback) {
  try {
    store[key] = await window.trader.get(route);
  } catch {
    if (fallback !== undefined) store[key] = fallback;
  }
}

function renderAll() {
  renderHeader();
  renderNav();
  renderTiles();
  renderLiveLine();
  renderLedger();
  renderBook();
  renderDecisions();
  renderResearch();
  renderRisk();
  renderBrief();
}

async function refresh() {
  try {
    const h = await window.trader.get('/health');
    $('health').className = `pill ${h.db === 'ok' ? 'live' : 'bad'}`;
    $('health').replaceChildren(el('span', { className: 'd' }), h.db === 'ok' ? 'Connected' : `Database ${h.db}`);
    if (!apiUp) {
      apiUp = true;
      refreshResearch();
    }
  } catch {
    apiUp = false;
    $('health').className = 'pill bad';
    $('health').replaceChildren(el('span', { className: 'd' }), 'API down');
    renderLiveLine();
    return;
  }
  await Promise.all([
    load('/risk/status', 'risk'),
    load('/loop/status', 'loop'),
    load('/risk/approvals', 'approvals', []),
    load('/positions', 'positions', []),
    load('/orders?limit=50', 'orders', []),
    load('/decisions?limit=150', 'decisions', []),
    load('/audit?limit=60', 'audit', []),
    load('/usage?days=7', 'usage'),
  ]);
  renderAll();
  $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
}

async function refreshResearch() {
  await Promise.all([load('/research/view', 'view'), load('/research/notes?include_stale=true&limit=100', 'notes', [])]);
  renderResearch();
  renderBrief();
  renderNav();
}

$('pause-toggle').onclick = async () => {
  const pausing = !paused;
  const ok = await ask(
    pausing ? 'Pause trading?' : 'Resume trading?',
    pausing ? 'RiskGuard will deny every order, exits included, until you resume.' : 'The agent’s orders go back through the normal RiskGuard checks.',
    pausing ? 'Pause' : 'Resume',
  );
  if (!ok) return;
  try {
    await window.trader.post(pausing ? '/risk/pause?reason=trader-desk' : '/risk/resume');
    toast(pausing ? 'Trading paused' : 'Trading resumed');
  } catch (err) {
    toast(`${pausing ? 'Pause' : 'Resume'} failed: ${err.message}`, true);
  }
  refresh();
};

$('navToggle').onclick = () => setRail(!document.body.classList.contains('navopen'));
window.addEventListener('hashchange', renderNav);

window.trader.onBackendStatus(({ text, error }) => {
  if (error) toast(text, true);
  else if (!apiUp) $('health').textContent = text;
});

// Every second: relative times and the countdown, without rebuilding rows.
setInterval(() => {
  for (const node of document.querySelectorAll('[data-ts]')) if (node.dataset.ts) node.textContent = rel(node.dataset.ts);
  renderLiveLine();
}, 1000);

// Fetch's rule: narrow windows start on the rail and do not save the choice.
const NARROW = () => window.innerWidth < 1000;
let navPref = null;
try { navPref = localStorage.getItem('trader-desk:nav'); } catch { /* ignore */ }
setRail(!NARROW() && navPref !== 'rail');
renderNav();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(refreshResearch, RESEARCH_MS);
refreshBroker();
setInterval(refreshBroker, BROKER_MS);
