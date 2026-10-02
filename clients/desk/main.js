// Main process: bring up the trader backend if it isn't running, then open the window.
// All HTTP goes through here, so the page needs no CORS and never sees the API key.

const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn, execFile } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// The desk lives in clients/desk of a trader checkout, so by default it runs that checkout.
const TRADER_DIR = process.env.TRADER_DIR || path.resolve(__dirname, '..', '..');
// Trader's settings file. Default: the checkout's own .env. Point TRADER_DESK_ENV_FILE at a
// file outside every checkout to keep live settings away from the tests; the desk passes it
// to the backend it starts (env vars beat trader's .env).
const ENV_FILE = process.env.TRADER_DESK_ENV_FILE || path.join(TRADER_DIR, '.env');

function readEnvFile() {
  const out = {};
  try {
    for (const raw of fs.readFileSync(ENV_FILE, 'utf8').split('\n')) {
      const line = raw.trim();
      if (!line || line.startsWith('#') || !line.includes('=')) continue;
      const i = line.indexOf('=');
      out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
  } catch {
    // no file: trader falls back to a .env in its checkout, if any
  }
  return out;
}
const PORT = Number(process.env.TRADER_DESK_PORT || 8080);
const BASE = `http://127.0.0.1:${PORT}`;

// The only state-changing calls the window may make. Everything else is GET.
const ALLOWED_POSTS = [/^\/risk\/pause(\?|$)/, /^\/risk\/resume$/, /^\/risk\/approvals\/\d+\/resolve\?/];

let win = null;
let api = null; // the uvicorn child, only when this app started it
let quitting = false;
let lastStatus = { text: 'Starting…', error: false };

// Apps started from the launcher don't get the shell's PATH, so look in the usual places.
function bin(name) {
  for (const dir of [path.join(os.homedir(), '.local', 'bin'), '/usr/local/bin', '/usr/bin']) {
    const p = path.join(dir, name);
    if (fs.existsSync(p)) return p;
  }
  return name;
}

// Values from trader's settings file (env vars win), so the desk follows whatever trader runs with.
function envValue(key) {
  return process.env[key] || readEnvFile()[key] || '';
}

// Match the API's auth: send the key from the settings file when one is set.
const apiKey = () => envValue('TRADER_API_KEY');

// Read-only views of the Alpaca paper account. Trader has no account endpoint yet, so the
// desk reads the broker directly; only these GET paths exist, so nothing here can trade.
const ALPACA = 'https://paper-api.alpaca.markets';
const BROKER_READS = {
  account: '/v2/account',
  positions: '/v2/positions',
  history: '/v2/account/portfolio/history?period=1M&timeframe=1D',
};

function status(text, error = false) {
  lastStatus = { text, error };
  win?.webContents.send('backend-status', lastStatus);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function healthy() {
  try {
    const r = await fetch(`${BASE}/health`, { signal: AbortSignal.timeout(1500) });
    return r.ok;
  } catch {
    return false;
  }
}

function run(cmd, args) {
  return new Promise((resolve, reject) =>
    execFile(cmd, args, { cwd: TRADER_DIR }, (err, _out, stderr) =>
      err ? reject(new Error(stderr.trim() || err.message)) : resolve(),
    ),
  );
}

async function ensureBackend() {
  if (await healthy()) return status(`Connected to running API on :${PORT}`);

  status('Starting database…');
  await run(bin('docker'), ['compose', '-f', 'infra/docker-compose.yml', 'up', '-d', '--wait', 'db']);

  status('Starting API…');
  api = spawn(
    bin('uv'),
    ['run', 'uvicorn', 'trader.surfaces.api.app:app', '--host', '127.0.0.1', '--port', String(PORT)],
    { cwd: TRADER_DIR, env: { ...readEnvFile(), ...process.env }, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  let tail = '';
  const keep = (chunk) => (tail = (tail + chunk).slice(-3000));
  api.stdout.on('data', keep);
  api.stderr.on('data', keep);
  api.on('exit', (code) => {
    api = null;
    if (!quitting) status(`API exited (code ${code})\n${tail}`, true);
  });

  for (let i = 0; i < 60; i++) {
    if (await healthy()) return status(`Started API on :${PORT}`);
    if (!api) return; // exit handler already reported it
    await sleep(500);
  }
  throw new Error(`API did not answer /health\n${tail}`);
}

ipcMain.handle('backend-status', () => lastStatus);

ipcMain.handle('broker', async (_event, view) => {
  const route = BROKER_READS[view];
  if (!route) throw new Error(`unknown broker view: ${view}`);
  const id = envValue('ALPACA_API_KEY_ID');
  const secret = envValue('ALPACA_API_SECRET');
  if (!id || !secret) throw new Error('no Alpaca paper keys in the settings file');
  const r = await fetch(ALPACA + route, {
    headers: { 'APCA-API-KEY-ID': id, 'APCA-API-SECRET-KEY': secret },
    signal: AbortSignal.timeout(10000),
  });
  // Status only: broker error bodies are not passed to the page.
  if (!r.ok) throw new Error(`Alpaca returned ${r.status}`);
  return r.json();
});

ipcMain.handle('api', async (_event, method, route) => {
  if (method !== 'GET' && !(method === 'POST' && ALLOWED_POSTS.some((re) => re.test(route)))) {
    throw new Error(`blocked: ${method} ${route}`);
  }
  const key = apiKey();
  const r = await fetch(BASE + route, { method, headers: key ? { 'X-API-Key': key } : {} });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error(body?.detail ? JSON.stringify(body.detail) : `${r.status} ${r.statusText}`);
  return body;
});

app.whenReady().then(() => {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    title: 'Salamander',
    backgroundColor: '#0a0a0f',
    autoHideMenuBar: true,
    icon: path.join(__dirname, 'brand', 'salamander-icon.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.js') },
  });
  win.loadFile('index.html');
  ensureBackend().catch((err) => status(err.message, true));
});

// Stop only the API this app started; the database container keeps running.
app.on('before-quit', () => {
  quitting = true;
  api?.kill('SIGTERM');
});

app.on('window-all-closed', () => app.quit());
