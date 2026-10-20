// Bots page (#127): each Bot's current version, Symbol modes, Sleeve and Sources, and one
// switch: poll sentiment for the symbols it does not hold. The switch goes through the same
// service as the CLI and MCP (`set_sentiment_poll`), so every flip is a new, audited Bot
// version. Every other Bot command stays on the CLI.

const SENTIMENT_COST =
  'Sentiment polling is the biggest LLM cost: it was 98% of model spend when it was not limited to held symbols. ' +
  'Each extra symbol adds one more poll per cadence (every 3 hours by default).';

const bots = { list: [], busy: null };

async function refreshBots() {
  try {
    bots.list = await window.trader.get('/bots');
  } catch {
    // keep the last list: an API blip should not blank the page
  }
}

const MODE_PILL = { live: 'pill live', shadow: 'pill', replay: 'pill warn' };

function symbolLine(spec) {
  const entries = Object.entries(spec.symbols || {});
  if (!entries.length) return el('span', { className: 'sub', textContent: 'No symbols' });
  return el('span', { className: 'chainline' }, ...entries.map(([ticker, mode]) =>
    el('span', { className: MODE_PILL[mode] || 'pill', title: `${ticker} is in ${mode}` }, mono(ticker), ` ${mode}`)));
}

function sleeveText(bot) {
  const s = bot.sleeve || {};
  const held = (s.positions || []).filter((p) => Number(p.qty) !== 0).length;
  const capital = s.capital_usd == null ? 'the whole account' : usd(s.capital_usd, 0);
  const paused = s.risk?.trading_paused ? ' · paused' : s.risk?.halted_reason ? ' · halted' : '';
  return `Sleeve ${capital} · ${held} held${paused}`;
}

function sourcesText(spec) {
  const names = (spec.sources || []).map((s) => words(s.source));
  return names.length ? names.join(', ') : 'none';
}

async function flipSentiment(bot, want) {
  const state = botSentiment(bot);
  if (want) {
    const n = state.unheld;
    const ok = await ask(
      `Poll sentiment for ${bot.name}'s symbols not held?`,
      `${n} more symbol${n === 1 ? '' : 's'} will be polled at the sentiment cadence. ${SENTIMENT_COST} This saves a new version of ${bot.name}.`,
      'Turn on',
    );
    if (!ok) return renderBots(true);
  }
  bots.busy = bot.name;
  renderBots(true);
  try {
    const out = await window.trader.post(`/bots/${bot.name}/sentiment-poll`, { poll_unheld: want });
    toast(out.version === bot.version ? `${bot.name} unchanged at v${out.version}` : `${bot.name} is at v${out.version}`);
  } catch (err) {
    toast(errorText(err), true);
  }
  bots.busy = null;
  await refreshBots();
  renderBots(true);
}

function sentimentSwitch(bot) {
  const state = botSentiment(bot);
  const box = el('input', {
    type: 'checkbox',
    role: 'switch',
    checked: state.on,
    disabled: !state.enabled || bots.busy === bot.name,
    ariaLabel: `Poll sentiment for symbols not held: ${bot.name}`,
  });
  box.onchange = () => flipSentiment(bot, box.checked);
  return el('div', { className: 'botswitch' },
    el('div', { className: 'chainline' },
      el('label', { className: `switch${state.enabled ? '' : ' off'}` }, box, el('span', { className: 'track' }),
        el('span', { textContent: 'Poll sentiment for symbols not held' })),
      state.on ? el('span', { className: 'pill live', textContent: `on · ${state.unheld} unheld polled` }) : null),
    el('div', { className: 'sub', textContent: state.why ? `${state.why} ${SENTIMENT_COST}` : SENTIMENT_COST }));
}

function botRow(bot) {
  const spec = bot.spec || {};
  return el('div', { className: 'role', dataset: { bot: bot.name } },
    el('div', { className: 'rolehead' },
      el('b', { className: 'rolename', textContent: bot.name }),
      el('span', { className: 'pill', textContent: `v${bot.version}` }),
      el('span', { className: 'sub', textContent: sleeveText(bot) }),
      el('span', { className: 'spacer' }),
      el('span', { className: 'sub', textContent: `prompt ${spec.prompt?.variant || '—'}` })),
    el('div', { className: 'chain' },
      symbolLine(spec),
      el('div', { className: 'sub', textContent: `Sources: ${sourcesText(spec)}` }),
      sentimentSwitch(bot)));
}

// Rebuilt every refresh; not while a confirm dialog is open over it.
function renderBots(force = false) {
  if (!force && $('confirm').open) return;
  $('bots').replaceChildren(...(bots.list.length
    ? bots.list.map(botRow)
    : [empty('No Bots yet', 'This API is older than the Bots page, or it is not reachable.')]));
}
