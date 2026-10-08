// Models page (#106): Providers, each Role's Chain, Spend caps and Profiles. Everything here
// goes through the same service layer as the CLI and MCP; the API Probes a Chain before it
// saves, so a refused save shows the Probe's words and leaves the old Chain serving.
// Keys are typed into a password field, sent once in a POST body and never read back.

const ROLES = ['decide', 'analyst', 'critic', 'brief', 'compiler', 'sentiment', 'confirm'];
const models = {
  roles: null, caps: {}, providers: [], presets: {}, profiles: [],
  modelIds: {}, // provider name -> [{id, price_in, price_out, source}]
  probes: {}, // role -> probe_role result
  tests: {}, // provider name -> {ok, text}
  estimates: {}, // role -> estimate from the last Chain save
  history: {}, // profile name -> GET /profiles/{name}, while open
  found: null, // local model servers from discover
  editing: null, // {kind, key, ...draft}: the one open editor
  refusal: null, // inline text under the open editor
  add: { preset: 'ollama', name: '', api_key: '', base_url: '' },
};

const activeProfile = () => models.profiles.find((p) => p.active) || null;

async function refreshModels() {
  const get = async (route, fallback) => {
    try { return await window.trader.get(route); } catch { return fallback; }
  };
  const [roles, caps, providers, profiles] = await Promise.all([
    get('/roles', null), get('/caps', {}), get('/providers', []), get('/profiles', []),
  ]);
  Object.assign(models, { roles, caps, providers, profiles });
  if (!Object.keys(models.presets).length) models.presets = await get('/providers/presets', {});
  for (const name of Object.keys(models.history)) models.history[name] = await get(`/profiles/${name}`, null);
}

async function modelIds(provider) {
  if (!models.modelIds[provider]) {
    try {
      models.modelIds[provider] = await window.trader.get(`/providers/${provider}/models`);
    } catch (err) {
      models.modelIds[provider] = [];
      toast(`${provider}: ${errorText(err)}`, true);
    }
  }
  return models.modelIds[provider];
}

// Run a write, report it, refresh, re-render. Returns the body, or null when refused.
async function act(label, call, { inline = false } = {}) {
  try {
    const body = await call();
    models.refusal = null;
    toast(label);
    await refreshModels();
    renderModels(true);
    return body;
  } catch (err) {
    const why = errorText(err);
    if (inline) models.refusal = why;
    toast(`${why}`, true);
    renderModels(true);
    return null;
  }
}

const btn = (text, onclick, extra = {}) => el('button', { textContent: text, onclick, type: 'button', ...extra });
const input = (props, onInput) => {
  const node = el('input', props);
  node.oninput = () => onInput(node.value);
  return node;
};
const editingIs = (kind, key) => models.editing && models.editing.kind === kind && models.editing.key === key;
function openEditor(kind, key, draft = {}) {
  models.editing = editingIs(kind, key) ? null : { kind, key, ...draft };
  models.refusal = null;
  renderModels(true);
}

// ---------- Roles ----------
function chainLine(entry, result) {
  const probe = result ? probeText(result) : null;
  return el('div', { className: 'chainline' },
    mono(`${entry.provider}:${entry.model}`),
    el('span', { className: 'sub', textContent: priceText(entry.price) }),
    probe ? el('span', { className: `pill ${probe.ok ? 'ok' : 'bad'}`, textContent: probe.text }) : null);
}

function roleRow(role) {
  const route = (models.roles || {})[role] || { source: '.env' };
  const onProfile = route.source === 'profile';
  const results = models.probes[role]?.probe || [];
  const chain = onProfile ? route.chain : [];
  const where = onProfile ? `${route.profile} v${route.version}` : '.env';
  const head = el('div', { className: 'rolehead' },
    el('b', { className: 'rolename', textContent: role }),
    el('span', { className: 'pill', textContent: where }),
    el('span', { className: 'sub capline', textContent: capText(models.caps[role]) }),
    el('span', { className: 'spacer' }),
    btn('Test', () => act(`Probed ${role}`, async () => {
      models.probes[role] = await window.trader.post(`/roles/${role}/probe`);
    }), { disabled: !onProfile, title: onProfile ? 'Probe each model in this Chain (one small call each)' : `${role} serves from .env` }),
    btn('Edit Chain', () => openEditor('chain', role, {
      chain: chain.length ? chain.map((e) => ({ provider: e.provider, model: e.model })) : [{ provider: models.providers[0]?.name || '', model: '' }],
    }), { disabled: !activeProfile() }),
    btn('Cap', () => openEditor('cap', role, { daily_usd: models.caps[role]?.cap_usd ?? '' })));
  const lines = onProfile
    ? (chain.length ? chain.map((e, i) => chainLine(e, results[i])) : [el('div', { className: 'sub', textContent: 'Off: empty Chain' })])
    : [el('div', { className: 'sub', textContent: 'Serves from .env' })];
  const row = el('div', { className: 'role', dataset: { role } }, head, el('div', { className: 'chain' }, ...lines));
  if (editingIs('chain', role)) row.append(chainEditor(role));
  if (editingIs('cap', role)) row.append(capEditor(role));
  return row;
}

function chainEditor(role) {
  const draft = models.editing;
  const profile = activeProfile();
  const list = `ids-${role}`;
  const rows = draft.chain.map((entry, i) => {
    const pick = el('select', { ariaLabel: `Provider ${i + 1}` },
      ...models.providers.map((p) => el('option', { value: p.name, textContent: p.name, selected: p.name === entry.provider })));
    pick.onchange = async () => {
      entry.provider = pick.value;
      await modelIds(entry.provider);
      renderModels(true);
    };
    const ids = models.modelIds[entry.provider] || [];
    const model = input({ value: entry.model, placeholder: 'model id (free text allowed)', ariaLabel: `Model ${i + 1}` }, (v) => { entry.model = v; });
    model.setAttribute('list', `${list}-${i}`);
    const options = el('datalist', { id: `${list}-${i}` }, ...ids.map((m) => el('option', { value: m.id, label: priceText(m) })));
    const chosen = ids.find((m) => m.id === entry.model);
    return el('div', { className: 'editrow' },
      el('span', { className: 'sub', textContent: i === 0 ? 'serves' : `fallback ${i}` }),
      pick, model, options,
      el('span', { className: 'sub', textContent: chosen ? priceText(chosen) : '' }),
      draft.chain.length > 1 ? btn('Remove', () => { draft.chain.splice(i, 1); renderModels(true); }) : null);
  });
  if (!models.modelIds[draft.chain[0]?.provider] && draft.chain[0]?.provider) modelIds(draft.chain[0].provider).then(() => renderModels(true));
  const estimate = models.estimates[role];
  return el('div', { className: 'editor' },
    el('div', { className: 'sub', textContent: `New version of ${profile ? profile.name : '—'}. Each model is Probed first; a failing first model saves nothing.` }),
    ...rows,
    el('div', { className: 'actions' },
      btn('Add fallback', () => { draft.chain.push({ provider: models.providers[0]?.name || '', model: '' }); renderModels(true); }),
      role === 'confirm' ? btn('Turn off', () => saveChain(role, [])) : null,
      btn('Use .env', () => act(`${role} serves from .env`, () => window.trader.del(`/profiles/${profile.name}/roles/${role}`)).then((ok) => { if (ok) models.editing = null; renderModels(true); })),
      btn('Probe and save', () => saveChain(role, draft.chain), { className: 'primary' })),
    models.refusal ? el('div', { className: 'refusal', textContent: models.refusal }) : null,
    estimate ? el('div', { className: 'sub', textContent: `Estimate ${usd(estimate.usd_per_day)} a day (${estimate.basis})` }) : null);
}

async function saveChain(role, chain) {
  const profile = activeProfile();
  const saved = await act(`Saved ${role} Chain`, () => window.trader.put(`/profiles/${profile.name}/roles/${role}`, { chain }), { inline: true });
  if (!saved) return;
  models.probes[role] = { probe: saved.probe };
  if (saved.estimate) models.estimates[role] = saved.estimate;
  for (const w of saved.warnings || []) toast(w, true);
  models.editing = null;
  renderModels(true);
}

function capEditor(role) {
  const draft = models.editing;
  const estimate = models.estimates[role];
  return el('div', { className: 'editor' },
    el('div', { className: 'editrow' },
      el('span', { className: 'sub', textContent: 'USD per UTC day' }),
      input({ value: draft.daily_usd, type: 'number', min: '0', step: '0.5', ariaLabel: `${role} daily cap` }, (v) => { draft.daily_usd = v; }),
      el('span', { className: 'sub', textContent: estimate ? `Chain estimate ${usd(estimate.usd_per_day)} a day` : 'Save the Chain to see its estimate' })),
    el('div', { className: 'actions' },
      btn('Clear', () => act(`${role} cap cleared`, () => window.trader.del(`/caps/${role}`)).then(() => { models.editing = null; renderModels(true); })),
      btn('Set cap', () => act(`${role} capped`, () => window.trader.put(`/caps/${role}`, { daily_usd: Number(draft.daily_usd) }), { inline: true })
        .then((ok) => { if (ok) models.editing = null; renderModels(true); }), { className: 'primary' })),
    models.refusal ? el('div', { className: 'refusal', textContent: models.refusal }) : null);
}

// ---------- Providers ----------
function providerRow(p) {
  const test = models.tests[p.name];
  const row = el('div', { className: 'role', dataset: { provider: p.name } },
    el('div', { className: 'rolehead' },
      el('b', { className: 'rolename', textContent: p.name }),
      el('span', { className: 'pill', textContent: p.local ? `${p.preset} · local` : p.preset }),
      mono(p.base_url),
      el('span', { className: 'sub', textContent: p.key || 'no key' }),
      test ? el('span', { className: `pill ${test.ok ? 'ok' : 'bad'}`, textContent: test.text }) : null,
      el('span', { className: 'spacer' }),
      btn('Test', async () => {
        delete models.modelIds[p.name];
        try {
          const ids = await window.trader.get(`/providers/${p.name}/models`);
          models.modelIds[p.name] = ids;
          models.tests[p.name] = { ok: true, text: `ok · ${ids.length} models` };
        } catch (err) {
          models.tests[p.name] = { ok: false, text: `failed · ${errorText(err)}` };
        }
        renderModels(true);
      }),
      btn('Replace key', () => openEditor('key', p.name, { api_key: '' })),
      btn('Remove', async () => {
        if (!(await ask(`Remove ${p.name}?`, 'Its stored key is deleted. A Provider in a current Chain cannot be removed.', 'Remove'))) return;
        act(`Removed ${p.name}`, () => window.trader.del(`/providers/${p.name}`));
      }, { className: 'danger' })));
  if (editingIs('key', p.name)) {
    const draft = models.editing;
    row.append(el('div', { className: 'editor' },
      el('div', { className: 'editrow' },
        input({ type: 'password', autocomplete: 'off', placeholder: 'new API key', ariaLabel: `${p.name} key` }, (v) => { draft.api_key = v; }),
        btn('Save key', async () => {
          const key = draft.api_key;
          draft.api_key = '';
          if (await act(`Key replaced for ${p.name}`, () => window.trader.post(`/providers/${p.name}/key`, { api_key: key }), { inline: true })) models.editing = null;
          renderModels(true);
        }, { className: 'primary' })),
      models.refusal ? el('div', { className: 'refusal', textContent: models.refusal }) : null));
  }
  return row;
}

function addProviderForm() {
  const a = models.add;
  const preset = models.presets[a.preset] || {};
  const pick = el('select', { ariaLabel: 'Preset' },
    ...Object.keys(models.presets).map((n) => el('option', { value: n, textContent: n, selected: n === a.preset })));
  pick.onchange = () => { a.preset = pick.value; renderModels(true); };
  const found = models.found;
  return el('div', { className: 'editor addform' },
    el('div', { className: 'editrow' },
      pick,
      input({ value: a.name, placeholder: 'name (a-z, 0-9, - _)', ariaLabel: 'Name' }, (v) => { a.name = v; }),
      input({ value: a.base_url, placeholder: preset.base_url || 'base URL (…/v1)', ariaLabel: 'Base URL' }, (v) => { a.base_url = v; }),
      preset.needs_key === false ? null : input({ type: 'password', autocomplete: 'off', placeholder: 'API key', ariaLabel: 'API key' }, (v) => { a.api_key = v; }),
      btn('Add', async () => {
        const body = { name: a.name.trim(), preset: a.preset, api_key: a.api_key || null, base_url: a.base_url || null };
        a.api_key = '';
        if (await act(`Added ${body.name}`, () => window.trader.post('/providers', body), { inline: true })) Object.assign(a, { name: '', base_url: '' });
        renderModels(true);
      }, { className: 'primary' }),
      btn('Find local servers', async () => {
        try { models.found = await window.trader.get('/providers/discover'); } catch (err) { toast(errorText(err), true); }
        renderModels(true);
      })),
    found ? el('div', { className: 'chain' }, ...(found.length ? found.map((f) => el('div', { className: 'chainline' },
      mono(f.base_url), el('span', { className: 'sub', textContent: `${f.preset} · ${f.models} models` }),
      btn('Use', () => { Object.assign(a, { preset: f.preset, base_url: f.base_url, name: a.name || f.preset }); renderModels(true); }))) : [el('span', { className: 'sub', textContent: 'No local model server answered on the usual ports.' })])) : null,
    !models.editing && models.refusal ? el('div', { className: 'refusal', textContent: models.refusal }) : null);
}

// ---------- Profiles ----------
function profileRow(p) {
  const shown = models.history[p.name];
  const row = el('div', { className: 'role', dataset: { profile: p.name } },
    el('div', { className: 'rolehead' },
      el('b', { className: 'rolename', textContent: p.name }),
      p.active ? el('span', { className: 'pill live', textContent: 'active' }) : null,
      el('span', { className: 'sub', textContent: `v${p.version} · ${Object.keys(p.roles || {}).join(', ') || 'every Role on .env'}` }),
      el('span', { className: 'spacer' }),
      p.active ? null : btn('Switch to', async () => {
        if (!(await ask(`Switch to ${p.name}?`, 'Every Chain in it is Probed first. The next call of each Role uses it; nothing restarts.', 'Switch'))) return;
        act(`${p.name} is active`, () => window.trader.post(`/profiles/${p.name}/activate`));
      }, { className: 'primary' }),
      btn('Rename', () => openEditor('rename', p.name, { new_name: '' })),
      btn('Duplicate', () => openEditor('duplicate', p.name, { new_name: '' })),
      btn(shown ? 'Hide history' : 'History', async () => {
        if (shown) delete models.history[p.name];
        else models.history[p.name] = await window.trader.get(`/profiles/${p.name}`).catch(() => null);
        renderModels(true);
      })));
  for (const kind of ['rename', 'duplicate']) {
    if (!editingIs(kind, p.name)) continue;
    const draft = models.editing;
    row.append(el('div', { className: 'editor' }, el('div', { className: 'editrow' },
      input({ placeholder: kind === 'rename' ? 'new name' : 'name of the copy', ariaLabel: `${kind} ${p.name}` }, (v) => { draft.new_name = v; }),
      btn(kind === 'rename' ? 'Rename' : 'Duplicate', async () => {
        if (await act(`${kind === 'rename' ? 'Renamed' : 'Copied'} ${p.name}`, () => window.trader.post(`/profiles/${p.name}/${kind}`, { new_name: draft.new_name.trim() }), { inline: true })) {
          models.editing = null;
          if (kind === 'rename') delete models.history[p.name];
        }
        renderModels(true);
      }, { className: 'primary' })),
      models.refusal ? el('div', { className: 'refusal', textContent: models.refusal }) : null));
  }
  if (shown) {
    row.append(el('div', { className: 'chain history' }, ...shown.versions.map((v) => el('div', { className: 'chainline' },
      el('b', { textContent: `v${v.version}` }),
      el('span', { className: 'sub', textContent: Object.entries(v.roles).map(([r, c]) => `${r}: ${c.map((e) => `${e.provider}:${e.model}`).join(' → ') || 'off'}`).join(' · ') || 'every Role on .env' }),
      when(v.created_at),
      v.version === shown.version ? el('span', { className: 'pill', textContent: 'current' }) : btn('Revert', async () => {
        if (!(await ask(`Revert ${p.name} to v${v.version}?`, p.active ? 'Its Chains are Probed first. History is kept: this moves the pointer.' : 'History is kept: this moves the pointer.', 'Revert'))) return;
        act(`${p.name} is at v${v.version}`, () => window.trader.post(`/profiles/${p.name}/revert`, { version: v.version }));
      })))));
  }
  return row;
}

function createProfileForm() {
  const draft = editingIs('create', '') ? models.editing : null;
  if (!draft) return btn('New Profile', () => openEditor('create', '', { name: '' }));
  return el('div', { className: 'editor' }, el('div', { className: 'editrow' },
    input({ placeholder: 'Profile name', ariaLabel: 'New Profile name' }, (v) => { draft.name = v; }),
    btn('Create', async () => {
      if (await act(`Created ${draft.name}`, () => window.trader.post('/profiles', { name: draft.name.trim() }), { inline: true })) models.editing = null;
      renderModels(true);
    }, { className: 'primary' })),
    models.refusal ? el('div', { className: 'refusal', textContent: models.refusal }) : null);
}

// The page rebuilds every refresh; not while you are typing in it.
function renderModels(force = false) {
  const chip = $('modelChip');
  chip.textContent = modelChip(models.roles);
  chip.className = `pill ${models.roles ? 'live' : ''}`;
  const view = $('view-models');
  if (!force && view.contains(document.activeElement) && document.activeElement.matches('input, select')) return;
  const noProfile = !activeProfile();
  const intro = noProfile ? [empty('No Profile yet', 'Every Role serves from .env. Create a Profile below to pick models per Role.')] : [];
  $('roles').replaceChildren(...intro, ...ROLES.map(roleRow));
  $('providers').replaceChildren(...models.providers.map(providerRow), addProviderForm());
  $('profiles').replaceChildren(...models.profiles.map(profileRow), createProfileForm());
}
