// Let this process own the window, private map key and single phone adapter. The page can request
// a few validated actions, but cannot start processes or read files. Device operations share a
// queue, and quitting gives Python time to Clear.

import { app, BrowserWindow, dialog, ipcMain, Menu, net, protocol, session, shell } from 'electron';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import { parseEnv } from 'node:util';
import { IPhone } from '../backend/iphone.mjs';

// Serve a fixed list of local assets from a private origin, not arbitrary disk paths.
const page = 'spoofme://app/index.html';
const files = new Map([
  ['/index.html', '../src/index.html'], ['/main.js', '../src/main.js'],
  ['/style.css', '../src/style.css'],
  ['/vendor/leaflet.js', '../node_modules/leaflet/dist/leaflet.js'],
  ['/vendor/leaflet.css', '../node_modules/leaflet/dist/leaflet.css'],
]);
const attributionLinks = new Set(['https://www.openstreetmap.org/copyright', 'https://carto.com/attribution/']);
// Leaflet positions map elements with inline styles; scripts and images stay on the app origin.
const csp = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data:; connect-src 'none'; " +
  "object-src 'none'; base-uri 'none'; form-action 'none'; frame-src 'none'";

function readMapKey() {
  // Let an environment variable override the private .env file; the key never goes in source.
  if (Object.hasOwn(process.env, 'CARTO_API_KEY')) return process.env.CARTO_API_KEY.trim();
  try {
    return (parseEnv(readFileSync(new URL('../.env', import.meta.url), 'utf8')).CARTO_API_KEY ?? '').trim();
  } catch { return ''; }
}
const mapKey = readMapKey();

async function mapTile(pathname) {
  // Accept only bounded Voyager tile coordinates here, not a caller-supplied remote URL.
  const match = /^\/tiles\/(\d{1,2})\/(\d{1,7})\/(\d{1,7})(@2x)?\.png$/.exec(pathname);
  if (!match) return new Response('Not found', { status: 404 });
  // Zoom z has 2^z tiles along each axis; reject coordinates outside that grid.
  const [, z, x, y] = match;
  if (Number(z) > 20 || Number(x) >= 2 ** Number(z) || Number(y) >= 2 ** Number(z)) {
    return new Response('Not found', { status: 404 });
  }
  if (!mapKey) return new Response('Map key not configured', { status: 503 });

  // Add the CARTO key in the main process so page URLs and tile errors do not expose it.
  const url = new URL(`https://basemaps.cartocdn.com/rastertiles/voyager/${pathname.slice(7)}`);
  url.searchParams.set('key', mapKey);
  try {
    const response = await net.fetch(url.href, {
      headers: { 'User-Agent': `SpoofMe/${app.getVersion()} (personal macOS project)` },
    });
    // Preserve the tile cache rules and image data, but leave out unrelated provider headers.
    return new Response(response.body, { status: response.status, headers: {
      'Content-Type': response.headers.get('Content-Type') ?? 'image/png',
      'Cache-Control': response.headers.get('Cache-Control') ?? 'public, max-age=3600',
    } });
  } catch { return new Response('Map unavailable', { status: 502 }); }
}

// Register before Electron is ready so the private origin behaves consistently for local assets.
protocol.registerSchemesAsPrivileged([
  { scheme: 'spoofme', privileges: { standard: true, secure: true, supportFetchAPI: true } },
]);
app.setName('SpoofMe');

let window;
let phone;
let poll;
let allowQuit = false;
let polling = false;
let work = Promise.resolve();
// Keep preview history in memory; the Python recovery file holds no coordinates.
const state = {
  busy: false, closing: false, devices: [], deviceHandle: null, lastSent: null, error: '',
  mapConfigured: Boolean(mapKey),
  status: { prepared: false, locationState: 'idle', recoveryRequired: false, device: null },
};

function publish() {
  // Send display state to the window, keeping the adapter and Python process out of the page.
  if (window && !window.isDestroyed()) window.webContents.send('phone:state', state);
}

function unavailable(message) {
  // Make connection failures visible without claiming the phone has restored itself.
  state.error = message;
  state.status = { ...state.status, prepared: false, locationState: 'unknown' };
  publish();
}

async function ensurePhone() {
  // Restart a failed bridge only to reconnect and read recovery state, never to replay a point.
  if (phone?.child && !phone.failed && !phone.closing) return;
  if (phone) await phone.shutdown().catch(() => {});
  phone = new IPhone();
  state.devices = [];
  state.deviceHandle = null;
  state.status = await phone.start();
}

async function execute(action, data) {
  // Refresh can restart a failed bridge, but it never prepares or selects a phone.
  if (action === 'refresh') {
    await ensurePhone();
    state.devices = (await phone.discover()).devices;
    return;
  }
  if (!phone || phone.failed || phone.closing) throw new Error('Refresh the connection and select your iPhone again.');

  // Route button actions through the adapter, which keeps the prepared device handle.
  if (action === 'prepare') {
    if (phone.device !== data.device) state.lastSent = null;
    state.deviceHandle = null;
    state.status = await phone.prepare(data.device);
    state.deviceHandle = data.device;
  }
  // Update lastSent only after Set returns, keeping it separate from the draft pin.
  if (action === 'set') {
    state.status = await phone.setLocation(data.latitude, data.longitude);
    state.lastSent = { latitude: data.latitude, longitude: data.longitude };
  }
  if (action === 'clear') state.status = await phone.clearLocation();
  if (action === 'confirm') state.status = await phone.confirmRestored(true);
}

async function runAction(action, data = {}) {
  if (state.busy || state.closing) return { ok: false, error: 'Wait for the current operation to finish.' };
  state.busy = true;
  state.error = '';
  publish();

  // Queue a button action behind an existing status poll so the adapter gets one request at a
  // time.
  const job = work.then(() => execute(action, data));
  // Recover the queue after a failure while the original job still carries its error to the caller.
  work = job.catch(() => {});
  try {
    await job;
    return { ok: true, result: state };
  } catch (error) {
    state.error = error.message;
    if (phone?.failed || ['set', 'clear', 'confirm'].includes(action)) {
      state.status = { ...state.status, prepared: false, locationState: 'unknown' };
    }
    return { ok: false, error: state.error };
  } finally {
    state.busy = false;
    publish();
  }
}

function pollStatus() {
  if (phone?.failed && !state.closing) {
    if (state.status.prepared || !state.error) unavailable('The phone connection closed. Refresh and reconnect the same iPhone.');
    return;
  }
  // Poll only while idle, using the same promise chain as button actions.
  if (state.busy || state.closing || polling || !phone?.child || phone.failed || phone.closing) return;
  polling = true;
  const job = work.then(() => phone.status());
  work = job.catch(() => {});
  job.then((status) => { state.status = status; publish(); })
    .catch((error) => unavailable(error.message))
    .finally(() => { polling = false; });
}

function validateRequest(event, data) {
  // Accept IPC only from the app’s main frame and validate the exact fields for each action.
  if (event.sender !== window?.webContents || event.senderFrame !== window.webContents.mainFrame ||
      event.senderFrame.url !== page) throw new Error('This page cannot control the phone.');
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Invalid request.');
  // Exact field lists also reject accidental payload changes between the page and main process.
  const fields = {
    snapshot: [], refresh: [], prepare: ['device'], set: ['latitude', 'longitude'],
    clear: [], confirm: ['confirmed'],
  };
  if (typeof data.action !== 'string' || !Object.hasOwn(fields, data.action) ||
      Object.keys(data).sort().join(',') !== ['action', ...fields[data.action]].sort().join(',')) {
    throw new Error('Invalid request fields.');
  }
  if (data.action === 'prepare' &&
      (typeof data.device !== 'string' || !state.devices.some((device) => device.id === data.device))) {
    throw new Error('Choose a device from the current list.');
  }
  if (data.action === 'set' && (!Number.isFinite(data.latitude) || Math.abs(data.latitude) > 90 ||
      !Number.isFinite(data.longitude) || Math.abs(data.longitude) > 180)) {
    throw new Error('Enter valid latitude and longitude values.');
  }
  if (data.action === 'confirm' && data.confirmed !== true) throw new Error('Check Apple Maps before confirming.');
}

async function quit() {
  if (state.closing || allowQuit) return;
  state.closing = true;
  publish();
  try {
    // Ask Python to attempt Clear before it closes. An unconfirmed recovery record stays saved.
    if (phone) await phone.shutdown();
  } catch (error) {
    unavailable('Location restoration is unverified. Reconnect the same iPhone and run Restore.');
    const answer = await dialog.showMessageBox(window, {
      type: 'warning', title: 'Location still needs checking',
      message: 'Could not verify cleanup before quitting.',
      detail: 'The recovery record is kept. Check Apple Maps, then reconnect the same iPhone to restore its location.',
      buttons: ['Keep open', 'Quit'], defaultId: 0, cancelId: 0,
    });
    // Keep the app usable if cleanup needs another attempt instead of forcing it to exit.
    if (answer.response === 0) {
      state.closing = false;
      publish();
      return;
    }
  }
  allowQuit = true;
  clearInterval(poll);
  app.quit();
}

async function createWindow() {
  // Use a dedicated browser session and decline permissions the map does not need.
  const browser = session.fromPartition('persist:spoofme');
  browser.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  browser.setPermissionCheckHandler(() => false);

  // Serve the page, bundled Leaflet files and tile route from the private app origin.
  browser.protocol.handle('spoofme', async (request) => {
    const url = new URL(request.url);
    if (url.hostname !== 'app' || request.method !== 'GET' || url.search) return new Response('Not found', { status: 404 });
    if (url.pathname.startsWith('/tiles/')) return mapTile(url.pathname);
    const asset = files.get(url.pathname);
    if (!asset) return new Response('Not found', { status: 404 });
    const response = await net.fetch(new URL(asset, import.meta.url).href);
    const headers = new Headers(response.headers);
    headers.set('Content-Security-Policy', csp);
    return new Response(response.body, { status: response.status, headers });
  });

  // Block direct remote requests from the page. Only the main process fetches CARTO images.
  browser.webRequest.onBeforeRequest((details, callback) => {
    callback({ cancel: !details.url.startsWith('spoofme://app/') });
  });

  // Keep the renderer sandboxed and isolated, with Node integration switched off.
  window = new BrowserWindow({
    width: 1180, height: 800, minWidth: 940, minHeight: 680,
    title: 'SpoofMe', backgroundColor: '#f4f5f2', show: false,
    webPreferences: {
      preload: fileURLToPath(new URL('./preload.cjs', import.meta.url)),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
      webviewTag: false, session: browser,
    },
  });

  // Allow the two attribution links in the normal browser and block other navigation.
  const openAttribution = (url) => { if (attributionLinks.has(url)) void shell.openExternal(url); };
  window.webContents.on('will-navigate', (event, url) => { event.preventDefault(); openAttribution(url); });
  window.webContents.setWindowOpenHandler(({ url }) => { openAttribution(url); return { action: 'deny' }; });
  window.webContents.on('will-attach-webview', (event) => event.preventDefault());
  // Window close and Cmd+Q use the same asynchronous cleanup before allowing app exit.
  window.on('close', (event) => { if (!allowQuit) { event.preventDefault(); void quit(); } });
  window.webContents.on('render-process-gone', () => { void quit(); });
  window.once('ready-to-show', () => window.show());
  await window.loadURL(page);
  await runAction('refresh');
  poll = setInterval(pollStatus, 2000);
}

// Keep one app instance in charge of Python. The Python lock also prevents another bridge.
if (!app.requestSingleInstanceLock()) app.quit();
else {
  ipcMain.handle('phone:command', async (event, data) => {
    try {
      validateRequest(event, data);
      return data.action === 'snapshot' ? { ok: true, result: state } : await runAction(data.action, data);
    } catch (error) { return { ok: false, error: error.message }; }
  });
  app.on('second-instance', () => { window?.show(); window?.focus(); });
  app.on('before-quit', (event) => { if (!allowQuit) { event.preventDefault(); void quit(); } });
  app.on('window-all-closed', () => { if (!allowQuit) void quit(); });
  // Wait for Electron before registering the session and loading the local page.
  app.whenReady().then(() => {
    Menu.setApplicationMenu(Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }]));
    return createWindow();
  }).catch(async () => { await phone?.shutdown().catch(() => {}); allowQuit = true; app.quit(); });
}
