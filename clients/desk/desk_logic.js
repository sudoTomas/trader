// Shared by the Electron main process (require) and the page (classic script).
// The page must not be able to aim the operator API key at another host.

const ALLOWED_POSTS = [
  /^\/risk\/pause(\?|$)/,
  /^\/risk\/resume$/,
  /^\/risk\/approvals\/\d+\/resolve\?/,
];

function deskRequestUrl(base, method, route, allowedPosts = ALLOWED_POSTS) {
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
  if (method === 'POST' && allowedPosts.some((re) => re.test(pathAndQuery))) return url;
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

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { ALLOWED_POSTS, deskRequestUrl, approvalToast, resolveAuditLine, criticPill, confirmPill };
}
