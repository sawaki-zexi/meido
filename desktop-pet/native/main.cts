import electron = require('electron');
import fs = require('node:fs');
import path = require('node:path');
import crypto = require('node:crypto');
import { pathToFileURL } from 'node:url';
const { app, BrowserWindow, ipcMain, protocol, screen } = electron;
type Binding = { roleId: string; packageId: string; generation: string; displayName: string;
  actions: Record<string, string>; spritePath: string; spriteSha256: string };
type Envelope = { type: string; nonce: string; roleId: string; packageId: string; generation: string;
  binding?: Binding; actionId?: string; arguments?: Record<string, string> };
type Flight = { id: string; moved: boolean; played: boolean; frames: number };
let binding: Binding | undefined;
let nonce = '';
let window: Electron.BrowserWindow | undefined;
let sprite: Buffer;
let flight: Flight | undefined;
let moveTimer: ReturnType<typeof setInterval> | undefined;
let input = Buffer.alloc(0);
let processing = false;
let ready = false;
let quitting = false;
// Electron v44 lib/common/init.ts replaces process.stdin with EOF on Windows.
// Read our inherited anonymous pipe through fd 0, without any listening port.
const ownerInput = fs.createReadStream('', { fd: 0, autoClose: false, highWaterMark: 16384 });
const rootArg = process.argv.indexOf('--runtime-root');
if (rootArg < 0 || !process.argv[rootArg + 1] || !path.isAbsolute(process.argv[rootArg + 1])) process.exit(2);
const runtimeRoot = process.argv[rootArg + 1];
app.setPath('userData', path.join(runtimeRoot, 'profile'));
app.setPath('sessionData', path.join(runtimeRoot, 'session'));
app.commandLine.appendSwitch('disable-background-networking');
protocol.registerSchemesAsPrivileged([{ scheme: 'pet-asset', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
const documentURL = pathToFileURL(path.join(__dirname, 'renderer/index.html')).href;

function geometry() {
  return window && !window.isDestroyed() ? { bounds: window.getBounds(), visible: window.isVisible(),
    alwaysOnTop: window.isAlwaysOnTop(), focusable: window.isFocusable(), transparent: true,
    displayId: screen.getDisplayMatching(window.getBounds()).id } : null;
}
function emit(type: string, fields: Record<string, unknown> = {}) {
  if (!binding) return;
  const raw = JSON.stringify({ type, nonce, roleId: binding.roleId, packageId: binding.packageId,
    generation: binding.generation, ...fields }) + '\n';
  if (Buffer.byteLength(raw) > 16384 || !process.stdout.write(raw)) {
    // A stalled owner cannot accumulate an unbounded event queue.
    void shutdown();
  }
}
function validSender(event: Electron.IpcMainEvent | Electron.IpcMainInvokeEvent) {
  return window && !window.isDestroyed() && event.sender === window.webContents
    && event.senderFrame === window.webContents.mainFrame && event.senderFrame.url === documentURL;
}
function clamp(point: { x: number; y: number }) {
  const work = screen.getDisplayNearestPoint(point).workArea;
  return { x: Math.round(Math.min(work.x + Math.max(0, work.width - 192), Math.max(work.x, point.x))),
    y: Math.round(Math.min(work.y + Math.max(0, work.height - 208), Math.max(work.y, point.y))) };
}
function savedPosition() {
  try {
    const raw = fs.readFileSync(path.join(runtimeRoot, 'position.json'), 'utf8');
    if (raw.length > 1024) throw new Error('position too large');
    const value = JSON.parse(raw) as { x: number; y: number };
    if (Number.isFinite(value.x) && Number.isFinite(value.y)) return clamp(value);
  } catch { /* A missing or invalid position uses the current work area. */ }
  const work = screen.getPrimaryDisplay().workArea;
  return clamp({ x: work.x + work.width - 208, y: work.y + work.height - 224 });
}
function savePosition() {
  if (!window || window.isDestroyed()) return;
  const { x, y } = window.getBounds();
  const target = path.join(runtimeRoot, 'position.json');
  const temporary = target + '.tmp';
  fs.writeFileSync(temporary, JSON.stringify({ x, y }), { mode: 0o600 });
  fs.renameSync(temporary, target);
}
function finish() {
  if (flight?.moved && flight.played) {
    savePosition();
    emit('finished', { actionId: flight.id, frames: flight.frames, window: geometry() });
    flight = undefined;
  }
}
function move(target: string) {
  const bounds = window!.getBounds();
  const work = screen.getDisplayMatching(bounds).workArea;
  const positions: Record<string, { x: number; y: number }> = {
    top_left: { x: work.x, y: work.y }, top_right: { x: work.x + work.width - 192, y: work.y },
    bottom_left: { x: work.x, y: work.y + work.height - 208 },
    bottom_right: { x: work.x + work.width - 192, y: work.y + work.height - 208 },
    center: { x: work.x + (work.width - 192) / 2, y: work.y + (work.height - 208) / 2 } };
  const end = clamp(positions[target]);
  const started = performance.now();
  moveTimer = setInterval(() => {
    if (!window || window.isDestroyed() || !flight) return;
    const progress = Math.min(1, (performance.now() - started) / 800);
    const point = clamp({ x: bounds.x + (end.x - bounds.x) * progress, y: bounds.y + (end.y - bounds.y) * progress });
    window.setPosition(point.x, point.y, false);
    if (progress === 1) {
      clearInterval(moveTimer); moveTimer = undefined;
      flight.moved = true; finish();
    }
  }, 16);
  return end.x < bounds.x ? 'running-left' : 'running-right';
}
async function bind(message: Envelope) {
  if (binding || !message.binding || !/^[a-f0-9]{48}$/.test(message.nonce)) throw new Error('invalid binding');
  const value = message.binding;
  if (['roleId', 'packageId', 'generation'].some((key) => message[key as keyof Envelope] !== value[key as keyof Binding])) throw new Error('binding identity');
  if (!path.isAbsolute(value.spritePath) || fs.statSync(value.spritePath).size > 33554432) throw new Error('invalid asset');
  sprite = fs.readFileSync(value.spritePath);
  if (crypto.createHash('sha256').update(sprite).digest('hex') !== value.spriteSha256) throw new Error('asset changed');
  binding = value; nonce = message.nonce;
  await app.whenReady();
  protocol.handle('pet-asset', (request) => request.url === 'pet-asset://package/sprite'
    ? new Response(new Uint8Array(sprite), { headers: { 'Content-Type': 'image/webp', 'Cache-Control': 'no-store' } })
    : new Response(null, { status: 404 }));
  const point = savedPosition();
  window = new BrowserWindow({ ...point, width: 192, height: 208, useContentSize: true, frame: false,
    transparent: true, alwaysOnTop: true, focusable: false, show: false, resizable: false, skipTaskbar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false,
      contextIsolation: true, sandbox: true, webSecurity: true, backgroundThrottling: false } });
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event) => event.preventDefault());
  window.webContents.on('will-attach-webview', (event) => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  window.webContents.session.setPermissionCheckHandler(() => false);
  window.webContents.on('render-process-gone', () => { emit('error', { reason: 'renderer exited' }); void shutdown(); });
  window.on('close', () => { try { savePosition(); } catch { /* Owner still receives close. */ } });
  window.on('closed', () => { emit('closed'); void shutdown(); });
  screen.on('display-removed', reposition);
  screen.on('display-metrics-changed', reposition);
  await window.loadURL(documentURL);
}
function reposition() {
  if (window && !window.isDestroyed()) {
    const point = clamp(window.getBounds()); window.setPosition(point.x, point.y, false);
  }
}
function action(message: Envelope) {
  if (!binding || !ready || !window || window.isDestroyed() || flight) throw new Error('host not ready');
  if (!message.actionId || !/^[a-f0-9]{32}$/.test(message.actionId)) throw new Error('invalid action id');
  const args = message.arguments;
  if (!args || !['play', 'move'].includes(args.action)) throw new Error('invalid action');
  let state: string;
  if (args.action === 'play') {
    state = binding.actions[args.name];
    if (!state || Object.keys(args).some((key) => !['action', 'name'].includes(key))) throw new Error('invalid play');
  } else {
    if (!['top_left', 'top_right', 'center', 'bottom_left', 'bottom_right'].includes(args.target)
      || !['', 'idle', 'run'].includes(args.animation ?? '')
      || Object.keys(args).some((key) => !['action', 'target', 'animation'].includes(key))) throw new Error('invalid move');
    state = 'idle';
  }
  flight = { id: message.actionId, moved: args.action !== 'move', played: false, frames: 0 };
  emit('accepted', { actionId: flight.id });
  if (args.action === 'move') {
    const direction = move(args.target);
    if (args.animation === 'run') state = direction;
  }
  window.webContents.send('pet:action', { actionId: flight.id, state });
}
async function receive(message: Envelope) {
  if (!binding) return bind(message);
  if (message.nonce !== nonce || ['roleId', 'packageId', 'generation'].some((key) => message[key as keyof Envelope] !== binding![key as keyof Binding])) throw new Error('stale host');
  if (message.type === 'action') return action(message);
  if (message.type === 'shutdown') return shutdown();
  throw new Error('unknown command');
}
async function drain() {
  if (processing) return;
  processing = true;
  try {
    while (input.includes(10)) {
      const end = input.indexOf(10);
      const raw = input.subarray(0, end); input = input.subarray(end + 1);
      if (raw.length > 16384) throw new Error('frame too large');
      await receive(JSON.parse(raw.toString('utf8')) as Envelope);
    }
  } catch (error) {
    process.stderr.write(String(error).slice(0, 1024) + '\n');
    emit('error', { ...(flight ? { actionId: flight.id } : {}), reason: 'invalid host command or native failure' });
    void shutdown();
  } finally { processing = false; }
}
async function shutdown() {
  if (quitting) return;
  quitting = true;
  clearInterval(moveTimer);
  if (app.isReady()) {
    screen.removeListener('display-removed', reposition);
    screen.removeListener('display-metrics-changed', reposition);
  }
  ownerInput.destroy();
  if (window && !window.isDestroyed()) window.destroy();
  app.quit();
}
ipcMain.handle('pet:initial', (event) => {
  if (!validSender(event) || !binding) throw new Error('invalid renderer');
  return { title: binding.displayName };
});
ipcMain.on('pet:ready', async (event) => {
  if (!validSender(event) || ready) return;
  ready = true; window!.showInactive();
  // Electron's default floating level moves Windows behind the taskbar and
  // can clear TOPMOST; keep the companion above ordinary windows explicitly.
  window!.setAlwaysOnTop(true, 'screen-saver');
  try {
    if (process.argv.includes('--acceptance-capture')) {
      // Opt-in acceptance captures this webContents only, never the desktop.
      const frame = await window!.webContents.capturePage();
      fs.writeFileSync(path.join(runtimeRoot, 'own-window.png'), frame.toPNG());
    }
    emit('ready', { window: geometry() });
  } catch { emit('error', { reason: 'own-window capture failed' }); void shutdown(); }
});
ipcMain.on('pet:receipt', (event, value: { actionId: string; status: string; frames: number }) => {
  if (!validSender(event) || !flight || !value || value.actionId !== flight.id
    || !['started', 'finished'].includes(value.status) || !Number.isInteger(value.frames) || value.frames < 1 || value.frames > 100) return;
  if (value.status === 'started') emit('started', { actionId: flight.id, frames: value.frames, window: geometry() });
  else { flight.played = true; flight.frames = value.frames; finish(); }
});
ipcMain.on('pet:close', (event) => { if (validSender(event)) void shutdown(); });
app.on('window-all-closed', () => app.quit());
ownerInput.on('data', (value) => {
  const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
  if (input.length + chunk.length > 32768) { void shutdown(); return; }
  input = Buffer.concat([input, chunk]); void drain();
});
ownerInput.on('end', () => void shutdown());
ownerInput.on('error', () => void shutdown());
process.on('uncaughtException', (error) => { process.stderr.write(String(error).slice(0, 1024) + '\n');
  emit('error', { reason: 'native host failure' }); void shutdown(); });
