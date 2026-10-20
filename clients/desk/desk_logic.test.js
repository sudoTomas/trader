const test = require('node:test');
const assert = require('node:assert/strict');
const {
  deskRequestUrl, approvalToast, resolveAuditLine, criticPill, confirmPill,
  modelChip, priceText, capText, probeText, servedText, errorText, stopText, stopSummary, configSections,
  botSentiment,
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

test('a holding shows its stop and how far the price is from it', () => {
  const stops = { stop_loss_frac: 0.08, cooldown_seconds: 86400, unprotected: ['ETH/USD'], positions: [
    { symbol: 'BTC/USD', qty: 0.5, avg_entry_price: 200, stop_price: 184 },
    { symbol: 'ETH/USD', qty: 2, avg_entry_price: null, stop_price: null },
    { symbol: 'SPY', qty: -3, avg_entry_price: 500, stop_price: 540 },
  ] };
  // Alpaca spells crypto without the slash.
  assert.deepEqual(stopText(stops, 'BTCUSD', 200), { warn: false, text: '$184.00 · 8.0% away' });
  assert.deepEqual(stopText(stops, 'ETHUSD', 3000), { warn: true, text: 'unprotected: entry unknown' });
  assert.deepEqual(stopText(stops, 'SPY', 520), { warn: false, text: '$540.00 · 3.8% away' });
  assert.equal(stopText({ ...stops, positions: [{ symbol: 'BTC/USD', qty: 0.01, stop_price: 58880 }] }, 'BTCUSD', 61000).text, '$58,880.00 · 3.5% away');
  assert.deepEqual(stopText(stops, 'BTCUSD', 180), { warn: true, text: '$184.00 · past the stop' });
  assert.equal(stopText(stops, 'QQQ', 400), null);
  assert.deepEqual(stopText({ ...stops, stop_loss_frac: 0 }, 'BTCUSD', 200), { warn: true, text: 'stop off' });
});

test('the stop setting reads as one line', () => {
  assert.equal(stopSummary({ stop_loss_frac: 0.08, cooldown_seconds: 86400 }), '8% loss from entry · no reopen for 24h');
  assert.equal(stopSummary({ stop_loss_frac: 0.05, cooldown_seconds: 5400 }), '5% loss from entry · no reopen for 1.5h');
  assert.equal(stopSummary({ stop_loss_frac: 0, cooldown_seconds: 86400 }), 'Off');
  assert.equal(stopSummary(null), null);
});

test('settings group by section and flag what differs from the default', () => {
  const fields = [
    { env_name: 'TRADER_LOOP_SYMBOLS', value: 'BTC/USD', default: 'ETH/USD,SPY', source: 'configured', section: 'Decide loop', category: 'tuning' },
    { env_name: 'TRADER_LOOP_TICK_SECONDS', value: 60, default: 60, source: 'default', section: 'Decide loop', category: 'tuning' },
    { env_name: 'TRADER_SHADOW_MODE', value: true, default: true, source: 'configured', section: 'Safety gates', category: 'gate' },
    { env_name: 'TRADER_API_KEY', value: 'set', default: 'not set', source: 'configured', section: 'Surfaces / auth', category: 'credential' },
  ];
  const out = configSections(fields);
  assert.deepEqual(out.map((s) => s.section), ['Decide loop', 'Safety gates', 'Surfaces / auth']);
  assert.deepEqual(out[0].rows.map((r) => [r.env_name, r.value, r.changed]), [
    ['TRADER_LOOP_SYMBOLS', 'BTC/USD', true],
    ['TRADER_LOOP_TICK_SECONDS', '60', false],
  ]);
  // Set explicitly to its default value: configured, but not a change.
  assert.equal(out[1].rows[0].changed, false);
  assert.equal(out[1].rows[0].value, 'true');
  assert.equal(out[2].rows[0].changed, true);
  assert.equal(configSections(null).length, 0);
});

// ---------- Bots page (#127) ----------

test('the Bots page may flip a Bot\'s sentiment toggle and nothing else on /bots', () => {
  assert.equal(deskRequestUrl(BASE, 'POST', '/bots/bot-2-largecap/sentiment-poll').pathname, '/bots/bot-2-largecap/sentiment-poll');
  for (const [method, route] of [
    ['POST', '/bots'],
    ['POST', '/bots/bot-2-largecap/versions'],
    ['POST', '/bots/bot-2-largecap/symbol-mode'],
    ['PUT', '/bots/bot-2-largecap/sentiment-poll'],
    ['POST', '/bots/bot-2-largecap/sentiment-poll/extra'],
    ['POST', '/bots/../broker/flatten-all/sentiment-poll'],
  ]) {
    assert.throws(() => deskRequestUrl(BASE, method, route), /blocked/, `${method} ${route}`);
  }
});

const bot = (name, sources, symbols = { AAPL: 'shadow', MSFT: 'shadow', NVDA: 'replay' }, positions = []) => ({
  name, version: 3, spec: { symbols, sources }, sleeve: { capital_usd: 5000, positions },
});

test('the sentiment switch reads the subscription, and says why it cannot move', () => {
  const off = botSentiment(bot('bot-2', [{ source: 'sentiment', params: {} }]));
  assert.deepEqual(off, { on: false, enabled: true, why: null, unheld: 2 });
  const on = botSentiment(bot('bot-2', [{ source: 'sentiment', params: { poll_unheld: true } }], { AAPL: 'live', MSFT: 'shadow' }, [{ symbol: 'AAPL', qty: 3 }]));
  assert.equal(on.on, true);
  assert.equal(on.unheld, 1); // AAPL is held, so it is polled anyway
  const one = botSentiment(bot('bot-1', [{ source: 'macro_view', params: {} }]));
  assert.equal(one.enabled, false);
  assert.match(one.why, /follows Settings/);
  const none = botSentiment(bot('bot-3', [{ source: 'brief', params: {} }]));
  assert.deepEqual([none.on, none.enabled], [false, false]);
  assert.match(none.why, /does not read sentiment/);
});
