// Main process: attach to the trader API already running, then open the window.
// All HTTP goes through here, so the page needs no CORS and never sees the API key.
// A failed health check does not start a backend. Closing the window stops nothing.

const { app, BrowserWindow, ipcMain } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { deskRequestUrl, ALLOWED_WRITES } = require('./desk_logic');

// The desk lives in clients/desk of a trader checkout, so by default it reads that checkout's .env.
const TRADER_DIR = process.env.TRADER_DIR || path.resolve(__dirname, '..', '..');
// Trader's settings file. Default: the checkout's own .env. Point TRADER_DESK_ENV_FILE at a
// file outside every checkout to keep live settings away from the tests. The desk reads
// the API key and the Alpaca paper keys from it; env vars win.
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

let win = null;
let lastStatus = { text: 'Starting…', error: false };

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

async function healthy() {
  try {
    const r = await fetch(`${BASE}/health`, { signal: AbortSignal.timeout(1500) });
    return r.ok;
  } catch {
    return false;
  }
}

async function ensureBackend() {
  if (await healthy()) return status(`Connected to running API on :${PORT}`);
  status(`No API on :${PORT}. Start trader-api; this window will not start one.`, true);
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

ipcMain.handle('api', async (_event, method, route, body) => {
  const url = deskRequestUrl(BASE, method, route, ALLOWED_WRITES);
  const key = apiKey();
  // The audit trail names the desk as the surface behind each change.
  const headers = { 'X-Trader-Surface': 'desk', ...(key ? { 'X-API-Key': key } : {}) };
  const init = { method, redirect: 'error', headers };
  // A JSON body goes only with a write (a Provider's key travels here, once).
  if (body !== undefined && method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const r = await fetch(url, init);
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

app.on('window-all-closed', () => app.quit());
