const test = require('node:test');
const assert = require('node:assert/strict');
const {
  deskRequestUrl, approvalToast, resolveAuditLine, criticPill, confirmPill,
  modelChip, priceText, capText, probeText, servedText, errorText,
} = require('./desk_logic');

const BASE = 'http://127.0.0.1:8080';

test('GET and the three POSTs stay on the local API', () => {
  assert.equal(deskRequestUrl(BASE, 'GET', '/health').href, `${BASE}/health`);
  assert.equal(deskRequestUrl(BASE, 'POST', '/risk/pause').pathname, '/risk/pause');
  assert.equal(deskRequestUrl(BASE, 'POST', '/risk/resume').pathname, '/risk/resume');
  const resolve = deskRequestUrl(BASE, 'POST', '/risk/approvals/4/resolve?approved=true&resolved_by=trader-desk');
  assert.equal(resolve.pathname, '/risk/approvals/4/resolve');
  assert.match(resolve.search, /approved=true/);
});

test('a route cannot carry the API key off box', () => {
  for (const route of [
    'http://evil.example/steal',
    '//evil.example/steal',
    'http://127.0.0.1:8080@evil.example/steal',
  ]) {
    assert.throws(() => deskRequestUrl(BASE, 'GET', route), /blocked/);
  }
  // A leading @ is a path on the local API, not userinfo. The old BASE + route join was not.
  const local = deskRequestUrl(BASE, 'GET', '@evil.example/steal');
  assert.equal(local.hostname, '127.0.0.1');
  assert.equal(local.port, '8080');
});

// The Models page (#106): every model-picker write, each with its own method, and nothing else.
const MODEL_WRITES = [
  ['POST', '/providers'],
  ['POST', '/providers/openrouter/key'],
  ['POST', '/providers/openrouter/rename'],
  ['POST', '/providers/openrouter/prices'],
  ['POST', '/providers/openrouter/prices/clear'],
  ['DELETE', '/providers/openrouter'],
  ['POST', '/profiles'],
  ['POST', '/profiles/everyday/rename'],
  ['POST', '/profiles/everyday/duplicate'],
  ['PUT', '/profiles/everyday/roles/decide'],
  ['DELETE', '/profiles/everyday/roles/decide'],
  ['POST', '/profiles/everyday/activate'],
  ['POST', '/profiles/everyday/revert'],
  ['POST', '/roles/decide/probe'],
  ['PUT', '/caps/critic'],
  ['DELETE', '/caps/critic'],
];

test('every model-picker write is allowed with its own method', () => {
  for (const [method, route] of MODEL_WRITES) {
    assert.equal(deskRequestUrl(BASE, method, route).pathname, route, `${method} ${route}`);
  }
});

test('nothing else became writable', () => {
  for (const [method, route] of [
    ['POST', '/broker/flatten-all?confirm=FLATTEN'],
    ['POST', '/broker/reconcile'],
    ['POST', '/broker/adopt?confirm=ADOPT'],
    ['POST', '/loop/wake'],
    ['POST', '/research/notes'],
    ['POST', '/xai/probe'],
    ['DELETE', '/risk/pause'],
    ['PUT', '/risk/resume'],
    ['PATCH', '/providers/openrouter'],
    // the right route under the wrong method
    ['PUT', '/providers'],
    ['POST', '/caps/critic'],
    ['DELETE', '/profiles/everyday'],
    ['PUT', '/roles/decide/probe'],
    // a query string or an extra segment rides along on nothing new
    ['POST', '/profiles/everyday/activate?then=/broker/flatten-all'],
    ['POST', '/providers/openrouter/key/extra'],
    ['DELETE', '/providers/a%2F..%2Fbroker'],
    ['POST', '/providers/../broker/flatten-all'],
    ['PUT', '/profiles/everyday/roles/../../../broker/flatten-all'],
  ]) {
    assert.throws(() => deskRequestUrl(BASE, method, route), /blocked/, `${method} ${route}`);
  }
});

test('the header chip names the decide model, or .env', () => {
  assert.equal(modelChip(null), 'decide: —');
  assert.equal(modelChip({ decide: { source: '.env' } }), 'decide: .env');
  const roles = { decide: { source: 'profile', chain: [{ provider: 'spark', model: 'qwen3:8b' }, { provider: 'claude', model: 'claude-opus-5-5' }] } };
  assert.equal(modelChip(roles), 'decide: spark:qwen3:8b');
  assert.equal(modelChip({ decide: { source: 'profile', chain: [] } }), 'decide: off');
});

test('a price reads per 1M tokens with its source; local is free, unknown says so', () => {
  assert.equal(priceText({ price_in: 3, price_out: 15, source: 'catalog' }), '$3.00 / $15.00 per 1M · catalog');
  assert.equal(priceText({ price_in: 0.1, price_out: 0.32, source: 'provider' }), '$0.10 / $0.32 per 1M · provider');
  assert.equal(priceText({ price_in: 0, price_out: 0, source: 'local' }), 'free · local');
  assert.equal(priceText({ price_in: 0, price_out: 0, source: 'unknown' }), 'price unknown');
  assert.equal(priceText(null), 'price unknown');
});

test('spend reads against the cap and says where the cap comes from', () => {
  assert.equal(capText({ cap_usd: 1.5, source: 'default', spent_today: 0.4213 }), '$0.42 of $1.50 today · default cap');
  assert.equal(capText({ cap_usd: 2, source: 'set', spent_today: 2.5 }), '$2.50 of $2.00 today · at cap');
  assert.equal(capText({ cap_usd: null, source: null, spent_today: 0 }), '$0.00 today · no cap');
  assert.equal(capText(undefined), '—');
});

test('a Probe result reads as health and latency, or its error', () => {
  assert.deepEqual(probeText({ ok: true, latency_ms: 1240, error: null }), { ok: true, text: 'ok · 1.2s' });
  assert.deepEqual(probeText({ ok: false, latency_ms: 30, error: 'HTTP 503' }), { ok: false, text: 'failed · HTTP 503' });
});

test('a decision names the model that served it and the Profile version', () => {
  assert.equal(servedText({ served_by: 'spark:qwen3:8b', profile_version: 3 }), 'served by spark:qwen3:8b · Profile v3');
  assert.equal(servedText({ served_by: 'nemotron:nemotron-super', profile_version: null }), 'served by nemotron:nemotron-super · .env');
  assert.equal(servedText({}), null);
});

test('an API refusal reads as its own words', () => {
  const ipc = (m) => new Error(`Error invoking remote method 'api': Error: ${m}`);
  assert.equal(errorText(ipc('"spark:qwen3:8b failed its decide Probe: HTTP 503; nothing was saved"')), 'spark:qwen3:8b failed its decide Probe: HTTP 503; nothing was saved');
  assert.equal(errorText(ipc('403 Forbidden')), '403 Forbidden');
  assert.equal(errorText(new Error('blocked: POST /loop/wake')), 'blocked: POST /loop/wake');
});

test('a remote base keeps the same-origin rule', () => {
  const remote = 'http://192.168.1.20:8080';
  assert.equal(deskRequestUrl(remote, 'GET', '/health').href, `${remote}/health`);
  assert.equal(deskRequestUrl(remote, 'POST', '/risk/pause').hostname, '192.168.1.20');
  // Loopback is another host now, so a route aimed back at it is blocked too.
  assert.throws(() => deskRequestUrl(remote, 'GET', 'http://127.0.0.1:8080/health'), /blocked/);
  assert.throws(() => deskRequestUrl(remote, 'GET', 'http://192.168.1.20:9090/health'), /blocked/);
});

test('path tricks do not become an allowed POST', () => {
  assert.throws(() => deskRequestUrl(BASE, 'POST', '/risk/pause/../../broker/flatten-all'), /blocked/);
  assert.throws(() => deskRequestUrl(BASE, 'POST', '/broker/flatten-all'), /blocked/);
  assert.throws(() => deskRequestUrl(BASE, 'POST', '/risk/approvals/4/resolve'), /blocked/);
});

test('approval toast says approved only when the redeem submitted', () => {
  assert.deepEqual(approvalToast({ resolved: true, status: 'approved', redeemed: true }, true, 7), {
    ok: true,
    text: 'Approved #7',
  });
  assert.deepEqual(
    approvalToast({ resolved: true, status: 'approved', redeemed: false, reason: 'risk chain denied the intent' }, true, 7),
    { ok: false, text: '#7 not submitted (risk chain denied the intent)' },
  );
  assert.deepEqual(approvalToast({ resolved: false, status: 'not pending' }, true, 7), {
    ok: false,
    text: '#7 not pending',
  });
  assert.deepEqual(approvalToast({ resolved: true, status: 'denied' }, false, 7), {
    ok: true,
    text: 'Denied #7',
  });
});

test('the ledger uses status, not a missing approved flag', () => {
  assert.deepEqual(resolveAuditLine({ status: 'approved', redeemed: true }), {
    verb: 'Approval approved',
    extra: ' · submitted',
  });
  assert.deepEqual(resolveAuditLine({ status: 'approved', redeemed: false, reason: 'no fresh price' }), {
    verb: 'Approval approved',
    extra: ' · not submitted (no fresh price)',
  });
  assert.equal(resolveAuditLine({ status: 'denied' }).verb, 'Approval denied');
  assert.equal(resolveAuditLine({ approved: true }).verb, 'Approval denied');
});

test('the critic pill shows its verdict and stays quiet on a hold', () => {
  assert.equal(criticPill(null), null);
  assert.equal(criticPill({ critic: { verdict: 'skipped' } }), null);
  assert.deepEqual(criticPill({ critic: { verdict: 'veto', reason: 'thin' } }), { tone: 'bad', text: 'critic · veto', title: 'thin' });
  assert.equal(criticPill({ critic: { verdict: 'pass_through' } }).text, 'critic · pass through');
});

test('the Kev pill reads the Confirm and its skips', () => {
  assert.equal(confirmPill(null), null);
  const draft = { action: 'open', side: 'buy' };
  assert.deepEqual(confirmPill({ draft, skipped: 'HTTP 500' }), { tone: 'warn', text: 'Kev · skipped', title: 'HTTP 500' });
  const doubt = confirmPill({ draft, p_same_action: 0.31, prefers: 'hold', verdict: 'journal_only', model: 'kev-latest' });
  assert.equal(doubt.text, 'Kev · open 31% · prefers hold');
  assert.equal(doubt.tone, 'warn');
  const agree = confirmPill({ draft, p_same_action: 0.72, prefers: 'open' });
  assert.deepEqual([agree.text, agree.tone], ['Kev · open 72%', 'ok']);
  assert.equal(confirmPill({ draft, p_same_action: 0.6, prefers: 'other_side' }).text, 'Kev · open 60% · prefers other side');
});

test('every desk script compiles', () => {
  // Only the page runs in Playwright; this catches a syntax error in the main process too.
  const fs = require('node:fs');
  const path = require('node:path');
  const vm = require('node:vm');
  for (const f of fs.readdirSync(__dirname).filter((n) => n.endsWith('.js'))) {
    assert.doesNotThrow(() => new vm.Script(fs.readFileSync(path.join(__dirname, f), 'utf8'), { filename: f }), f);
  }
});
