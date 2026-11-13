// Shared by the Electron main process (require) and the page (classic script).
// The page must not be able to aim the operator API key at another host.

// Provider and Profile names: a-z, 0-9, '-', '_' (the API enforces the same). Roles: a-z.
const NAME = '[a-z0-9][a-z0-9_-]{0,39}';
const ROLE = '[a-z]+';
const exact = (path) => new RegExp(`^${path}$`);

// [method, pattern] pairs, matched against the normalized path and query. Every other write
// (flatten-all, reconcile, adopt, wake, notes) stays CLI-only.
const ALLOWED_WRITES = [
  ['POST', /^\/risk\/pause(\?|$)/],
  ['POST', /^\/risk\/resume$/],
  ['POST', /^\/risk\/approvals\/\d+\/resolve\?/],
  // The Models page (#106). Keys travel in the body, once; nothing returns them.
  ['POST', exact('/providers')],
  ['POST', exact(`/providers/${NAME}/(key|extra-body|rename|prices|prices/clear)`)],
  ['DELETE', exact(`/providers/${NAME}`)],
  ['POST', exact('/profiles')],
  ['POST', exact(`/profiles/${NAME}/(rename|duplicate|activate|revert)`)],
  ['PUT', exact(`/profiles/${NAME}/roles/${ROLE}`)],
  ['DELETE', exact(`/profiles/${NAME}/roles/${ROLE}`)],
  ['POST', exact(`/roles/${ROLE}/probe`)],
  ['PUT', exact(`/caps/${ROLE}`)],
  ['DELETE', exact(`/caps/${ROLE}`)],
  // The Bots page (#127): one switch per Bot. Every other Bot command stays CLI-only.
  ['POST', exact(`/bots/${NAME}/sentiment-poll`)],
];

function deskRequestUrl(base, method, route, allowedWrites = ALLOWED_WRITES) {
  const baseUrl = new URL(base.endsWith('/') ? base : `${base}/`);
  let url;
  try {
    url = new URL(route, baseUrl);
  } catch {
    throw new Error(`blocked: ${method} ${route}`);
  }
  if (
    url.protocol !== baseUrl.protocol ||
    url.hostname !== baseUrl.hostname ||
    url.port !== baseUrl.port ||
    url.username ||
    url.password
  ) {
    throw new Error(`blocked: ${method} ${route}`);
  }
  const pathAndQuery = `${url.pathname}${url.search}`;
  if (method === 'GET') return url;
  if (allowedWrites.some(([m, re]) => m === method && re.test(pathAndQuery))) return url;
  throw new Error(`blocked: ${method} ${route}`);
}

// Toast text for the resolve call. "Approved" only when the redeem submitted.
function approvalToast(body, wantApprove, id) {
  const reason = body && body.reason ? ` (${body.reason})` : '';
  if (!body || body.resolved !== true) {
    return { ok: false, text: `#${id} ${body?.status || 'not pending'}${reason}` };
  }
  if (!wantApprove) return { ok: true, text: `Denied #${id}` };
  if (body.redeemed === true) return { ok: true, text: `Approved #${id}` };
  return { ok: false, text: `#${id} not submitted${reason}` };
}

// Ledger row for a resolve_approval audit event. The payload has status, not approved.
function resolveAuditLine(payload) {
  const known = payload && (payload.status === 'approved' || payload.status === 'denied' || payload.status === 'not pending');
  const status = known ? payload.status : 'denied';
  let extra = '';
  if (payload && payload.redeemed === true) extra = ' · submitted';
  else if (payload && payload.redeemed === false) {
    extra = ` · not submitted${payload.reason ? ` (${payload.reason})` : ''}`;
  }
  return { verb: `Approval ${status}`, extra };
}

// Decision detail pills for the second opinions on a Draft. Null when there is none.
const pctText = (v) => `${Math.round(Number(v) * 100)}%`;

function criticPill(deskVerdicts) {
  const c = deskVerdicts && deskVerdicts.critic;
  if (!c || !c.verdict || c.verdict === 'skipped') return null;
  const tone = c.verdict === 'veto' ? 'bad' : c.verdict === 'shrink' ? 'warn' : 'ok';
  return { tone, text: `critic · ${c.verdict.replace(/_/g, ' ')}`, title: c.reason || '' };
}

// The Confirm: Jev's judgments and the open veto (new rows), or Kev's journal-only
// p_same_action (old rows, which keep their shape).
const JEV_VERDICTS = ['veto', 'pass', 'recorded'];

function confirmPill(confirm) {
  if (!confirm || !confirm.draft) return null;
  // A new-shape record always carries vetoed (a skip too); an old one never does.
  const jev = 'answers' in confirm || 'vetoed' in confirm || JEV_VERDICTS.includes(confirm.verdict);
  const who = jev ? 'Jev' : 'Kev';
  if (confirm.skipped) return { tone: 'warn', text: `${who} · skipped`, title: confirm.skipped };
  if (jev) {
    const reason = String(confirm.veto_reason || 'unspecified').replace(/_/g, ' ');
    const title = [confirm.model, confirm.questions_version].filter(Boolean).join(' · ');
    if (confirm.vetoed) return { tone: 'warn', text: `Jev · veto: ${reason}`, title };
    if (confirm.would_veto) return { tone: 'warn', text: `Jev · would veto: ${reason}`, title };
    if (confirm.verdict === 'recorded') return { tone: '', text: 'Jev · recorded', title };
    return { tone: 'ok', text: 'Jev · pass', title };
  }
  const agrees = confirm.prefers === confirm.draft.action;
  const prefers = String(confirm.prefers).replace(/_/g, ' ');
  return {
    tone: agrees ? 'ok' : 'warn',
    text: `Kev · ${confirm.draft.action} ${pctText(confirm.p_same_action)}${agrees ? '' : ` · prefers ${prefers}`}`,
    title: `${confirm.verdict || ''} · ${confirm.model || ''}`,
  };
}

// ---------- Models page (#106) ----------
const dollars = (v) => `$${Number(v || 0).toFixed(2)}`;

// Header chip: the model the next decide call goes to first.
function modelChip(roles) {
  const decide = roles && roles.decide;
  if (!decide) return 'decide: —';
  if (decide.source !== 'profile') return 'decide: .env';
  const first = (decide.chain || [])[0];
  return first ? `decide: ${first.provider}:${first.model}` : 'decide: off';
}

// USD per 1M tokens (in / out) and where the number came from.
function priceText(price) {
  if (!price || price.source === 'unknown') return 'price unknown';
  if (price.source === 'local') return 'free · local';
  return `${dollars(price.price_in)} / ${dollars(price.price_out)} per 1M · ${price.source}`;
}

function capText(cap) {
  if (!cap) return '—';
  const spent = dollars(cap.spent_today);
  if (cap.cap_usd == null) return `${spent} today · no cap`;
  const where = cap.spent_today >= cap.cap_usd ? 'at cap' : cap.source === 'default' ? 'default cap' : 'cap';
  return `${spent} of ${dollars(cap.cap_usd)} today · ${where}`;
}

function probeText(result) {
  if (result.ok) return { ok: true, text: `ok · ${(result.latency_ms / 1000).toFixed(1)}s` };
  return { ok: false, text: `failed · ${result.error}` };
}

// Ticker and Live tile: the decide model running now. The newest decision's served_by is
// authoritative; the usage rows (one per model per day) only stand in for decisions served
// from .env, which carry no served_by. "provider:model" -> model; models may contain ':'.
function decideModelName(decisions, usageRows) {
  const latest = (decisions || []).find((d) => d && d.served_by);
  if (latest) return latest.served_by.split(':').slice(1).join(':') || latest.served_by;
  const row = (usageRows || []).find((r) => r.role === 'decide');
  return row ? row.model : null;
}

// Decision detail: which model served it, and from which Profile version (.env when none).
function servedText(d) {
  if (!d || !d.served_by) return null;
  return `served by ${d.served_by} · ${d.profile_version != null ? `Profile v${d.profile_version}` : '.env'}`;
}

// The words of an API refusal: Electron's IPC prefix off, the JSON-quoted detail unquoted.
function errorText(err) {
  const m = String(err && err.message ? err.message : err).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
  try {
    const parsed = JSON.parse(m);
    return typeof parsed === 'string' ? parsed : m;
  } catch {
    return m;
  }
}

const stopUsd = (v) => `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// The deterministic stop (#74) beside a holding. Broker symbols drop the crypto slash.
function stopText(stops, brokerSymbol, last) {
  if (!stops) return null;
  const key = String(brokerSymbol).replace('/', '');
  const p = (stops.positions || []).find((x) => x.symbol.replace('/', '') === key);
  if (!p) return null;
  if (!(stops.stop_loss_frac > 0)) return { warn: true, text: 'stop off' };
  if (p.stop_price == null) return { warn: true, text: 'unprotected: entry unknown' };
  const gap = p.qty > 0 ? last - p.stop_price : p.stop_price - last;
  if (gap <= 0) return { warn: true, text: `${stopUsd(p.stop_price)} · past the stop` };
  return { warn: false, text: `${stopUsd(p.stop_price)} · ${((gap / last) * 100).toFixed(1)}% away` };
}

function stopSummary(stops) {
  if (!stops) return null;
  if (!(stops.stop_loss_frac > 0)) return 'Off';
  const hours = stops.cooldown_seconds / 3600;
  return `${+(stops.stop_loss_frac * 100).toFixed(2)}% loss from entry · no reopen for ${+hours.toFixed(1)}h`;
}

// The Settings page (#94): the running config by section, in the API's order, with the
// values that differ from their default flagged. Values arrive already redacted.
function configSections(fields) {
  const show = (v) => (v === '' ? '(empty)' : String(v));
  const sections = [];
  for (const f of fields || []) {
    let s = sections.find((x) => x.section === f.section);
    if (!s) sections.push((s = { section: f.section, rows: [] }));
    s.rows.push({ env_name: f.env_name, value: show(f.value), default: show(f.default), category: f.category, description: f.description, changed: show(f.value) !== show(f.default) });
  }
  return sections;
}

// The Bots page (#127): the "poll sentiment for symbols not held" switch. `unheld` counts
// the symbols it would add (shadow or live, not in the Sleeve); held ones are polled anyway.
function botSentiment(bot) {
  const sub = (bot?.spec?.sources || []).find((s) => s.source === 'sentiment');
  const on = !!(sub && sub.params && sub.params.poll_unheld === true);
  const held = new Set((bot?.sleeve?.positions || []).filter((p) => Number(p.qty) !== 0).map((p) => p.symbol));
  const unheld = Object.entries(bot?.spec?.symbols || {}).filter(([t, m]) => m !== 'replay' && !held.has(t)).length;
  if (bot?.name === 'bot-1') {
    return { on, enabled: false, why: 'bot-1 follows Settings: it polls sentiment for held symbols only.', unheld };
  }
  if (!sub) return { on: false, enabled: false, why: 'This Bot does not read sentiment.', unheld };
  return { on, enabled: true, why: null, unheld };
}

// The Calls panel (Telegram channel calls): the scorecard's gate figures as tiles. The gate
// is reported, not enforced; nothing here trades.
const finite = (v) => v != null && v !== '' && Number.isFinite(Number(v));
const pnlUsd = (v) => {
  const n = Number(v);
  const s = `$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return n > 0 ? `+${s}` : n < 0 ? `-${s}` : s;
};
const netPct = (v) => {
  const n = Number(v) * 100;
  return `${n > 0 ? '+' : ''}${n.toFixed(1)}%`;
};

function scorecardTiles(sc) {
  const c = sc || {};
  const count = (v) => String(finite(v) ? Number(v) : 0);
  const pnl = finite(c.sleeve_pnl_usd) ? Number(c.sleeve_pnl_usd) : null;
  return [
    { label: 'Tradeable calls', value: count(c.tradeable), tone: '', sub: `of ${count(c.calls)} calls · ${count(c.untradeable?.calls)} untradeable` },
    { label: 'TP1 rate', value: finite(c.tp1_rate) ? pctText(c.tp1_rate) : '—', tone: '', sub: `${count(c.tp1_before_stop)} reached TP1 before the stop` },
    { label: 'Sleeve P&L', value: pnl == null ? '—' : pnlUsd(pnl), tone: pnl > 0 ? 'live' : pnl < 0 ? 'bad' : '', sub: finite(c.net_return_mean) ? `mean net ${netPct(c.net_return_mean)} per closed call` : 'no closed calls yet' },
    { label: 'Live days', value: count(c.live_days), tone: '', sub: 'of 28 needed' },
    { label: 'Gate', value: c.gate_pass ? 'passed' : 'not yet', tone: c.gate_pass ? 'live' : 'warn', sub: 'reported, not enforced' },
  ];
}

// One row of the calls table. Prices keep up to 6 significant digits (crypto runs from
// 0.00001 to 100,000); a missing level is a dash, a call with no entry is at market.
const level = (v) => (finite(v) ? Number(v).toLocaleString('en-US', { maximumSignificantDigits: 6 }) : '—');

function callRow(c) {
  const l = c?.levels || {};
  const score = c?.score || null;
  const net = score && (score.status === 'closed' || score.status === 'open') ? score.result?.net_return : null;
  return {
    coin: l.coin || '?',
    side: l.side || '—',
    posted: c?.posted_at || null,
    entry: finite(l.entry) ? level(l.entry) : 'market',
    tps: (l.tps || []).filter(finite).map(level).join(' / ') || '—',
    stop: level(l.stop),
    status: score ? score.status : 'unscored',
    net: finite(net) ? netPct(net) : '—',
    tradeable: score ? score.tradeable === true : null,
  };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    ALLOWED_WRITES, deskRequestUrl, approvalToast, resolveAuditLine, criticPill, confirmPill,
    modelChip, priceText, capText, probeText, servedText, decideModelName, errorText, stopText, stopSummary, configSections,
    botSentiment, scorecardTiles, callRow,
  };
}
