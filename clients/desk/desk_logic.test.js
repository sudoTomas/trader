const test = require('node:test');
const assert = require('node:assert/strict');
const { deskRequestUrl, approvalToast, resolveAuditLine, criticPill, confirmPill } = require('./desk_logic');

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
