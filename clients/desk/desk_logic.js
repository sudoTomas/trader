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
  ['POST', exact(`/providers/${NAME}/(key|rename|prices|prices/clear)`)],
  ['DELETE', exact(`/providers/${NAME}`)],
  ['POST', exact('/profiles')],
  ['POST', exact(`/profiles/${NAME}/(rename|duplicate|activate|revert)`)],
  ['PUT', exact(`/profiles/${NAME}/roles/${ROLE}`)],
  ['DELETE', exact(`/profiles/${NAME}/roles/${ROLE}`)],
  ['POST', exact(`/roles/${ROLE}/probe`)],
  ['PUT', exact(`/caps/${ROLE}`)],
  ['DELETE', exact(`/caps/${ROLE}`)],
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

// Kev's Confirm (journal-only for now): how likely it found the Draft's own action.
function confirmPill(confirm) {
  if (!confirm || !confirm.draft) return null;
  if (confirm.skipped) return { tone: 'warn', text: 'Kev · skipped', title: confirm.skipped };
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

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    ALLOWED_WRITES, deskRequestUrl, approvalToast, resolveAuditLine, criticPill, confirmPill,
    modelChip, priceText, capText, probeText, servedText, errorText, stopText, stopSummary, configSections,
  };
}
