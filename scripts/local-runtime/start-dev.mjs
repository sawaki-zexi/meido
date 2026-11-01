import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir } from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

const args = process.argv.slice(2);
const controlIndex = args.indexOf('--shutdown-stdin');
if (controlIndex >= 0) args.splice(controlIndex, 1);
const rootIndex = args.indexOf('--code-root');
const selectedRoot = rootIndex < 0 ? fileURLToPath(new URL('../../', import.meta.url)) : args.splice(rootIndex, 2)[1];
if (!selectedRoot) throw new Error('--code-root requires a checkout path');
const root = path.resolve(selectedRoot);
const identity = process.platform === 'win32' ? root.toLowerCase() : root;
const base = 14000 + (createHash('sha256').update(identity).digest().readUInt32BE() % 10000) * 2;
const apiPort = Number(process.env.MEIDO_DEV_API_PORT ?? base);
const webPort = Number(process.env.MEIDO_DEV_WEB_PORT ?? base + 1);
const info = { apiPort, webPort, dataDir: path.join(root, '.data/development'),
  rolesDir: path.join(root, '.data/development/roles'),
  frontendUrl: `http://127.0.0.1:${webPort}/app`, backendUrl: `http://127.0.0.1:${apiPort}` };

async function main() {
  const service = args.find(arg => arg !== '--check') ?? 'all';
  if (!['api', 'web', 'all'].includes(service)) throw new Error('Use api, web, or --check');
  for (const port of [apiPort, webPort]) {
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid development port');
    if ([4288, 5288, 5289].includes(port)) throw new Error(`Port ${port} is reserved for the installed runtime`);
  }
  if (apiPort === webPort) throw new Error('Development API and web ports must differ');
  if (args.includes('--check')) { console.log(JSON.stringify(info)); return; }
  for (const port of service === 'api' ? [apiPort] : service === 'web' ? [webPort] : [apiPort, webPort]) {
    await new Promise((resolve, reject) => {
      const probe = net.createServer();
      probe.once('error', () => reject(new Error(`Port ${port} is busy. Select different MEIDO_DEV_API_PORT / MEIDO_DEV_WEB_PORT.`)));
      probe.listen(port, '127.0.0.1', () => probe.close(resolve));
    });
  }
  const environment = { ...process.env, MEIDO_PROJECT_ROOT: root, MEIDO_DATA_DIR: info.dataDir,
    MEIDO_ROLES_DIR: info.rolesDir, MEIDO_OWNER_KNOWLEDGE_DATA_DIR: path.join(info.dataDir, 'owner-knowledge'),
    MEIDO_BROWSER_USE_DATA_DIR: path.join(info.dataDir, 'browser-use'), MEIDO_WEB_PORT: String(webPort),
    MEIDO_API_ORIGIN: info.backendUrl, MEIDO_WEB_ORIGIN: `http://127.0.0.1:${webPort}`,
    MEIDO_FEISHU_REDIRECT_URI: `${info.backendUrl}/api/owner-knowledge/oauth/callback` };
  for (const directory of [info.dataDir, info.rolesDir, environment.MEIDO_OWNER_KNOWLEDGE_DATA_DIR]) await mkdir(directory, { recursive: true });
  console.log(`Development: ${info.frontendUrl}\nAPI: ${info.backendUrl}\nData: ${info.dataDir}`);
  const children = [];
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    if (controlIndex >= 0) process.stdin.destroy();
    for (const { child, api } of children) {
      if (child.exitCode !== null || child.signalCode !== null) continue;
      // Windows SIGTERM/taskkill terminates Python without cleanup. Only this
      // launcher's private stdin pipe asks its runner/reload child to drain.
      if (api) child.stdin.end('shutdown\n');
      else child.kill();
    }
  };
  const launch = (command, arguments_, api = false) => {
    const child = spawn(command, arguments_, { cwd: root, env: environment,
      stdio: [api ? 'pipe' : 'ignore', 'inherit', 'inherit'], windowsHide: true });
    children.push({ child, api });
    child.stdin?.on('error', error => { if (error.code !== 'EPIPE') console.error(error.message); });
    child.once('error', error => { console.error(error.message); process.exitCode = 1; stop(); });
    child.once('exit', code => { if (code && (api || !stopping)) process.exitCode = code; stop(); });
  };
  if (service !== 'web') launch(process.env.MEIDO_DEV_PYTHON ?? 'python',
    ['-m', 'backend.app.server', '--host', '127.0.0.1', '--port', String(apiPort), '--reload', '--shutdown-stdin'], true);
  if (service !== 'api') launch(process.execPath,
    [path.join(root, 'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--port', String(webPort), '--strictPort']);
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  // A controlling test or parent shell can close this owned launcher cleanly.
  if (controlIndex >= 0) {
    const control = createInterface({ input: process.stdin });
    control.on('line', line => { if (line.trim() === 'shutdown') stop(); });
    control.on('close', stop);
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
