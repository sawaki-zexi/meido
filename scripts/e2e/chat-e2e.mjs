// Real browser -> isolated Meido HTTP/SSE -> controlled provider and real tools.
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { spawn, execFileSync } from 'node:child_process';
import { openSync, closeSync, existsSync, readFileSync } from 'node:fs';
import { cp, mkdir, mkdtemp, symlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';

const directory = path.dirname(fileURLToPath(import.meta.url));
const sourceRoot = path.resolve(directory, '../..');
const fixtureBase = path.join(sourceRoot, '.data/chat-e2e');
let root, api, web;
const python = process.env.MEIDO_DEV_PYTHON || 'python';
const stamp = Date.now().toString(36);
const artifacts = path.join(fixtureBase, 'artifacts', stamp);
const mcpServer = 'e2e_' + stamp;
const mcpPrefix = `mcp_${mcpServer}__`;
const report = { cases: [], requests: [], browserErrors: [], startedAt: new Date().toISOString() };
const only = process.env.MEIDO_E2E_ONLY;
const selected = only ? new Set(only.split(',')) : null;
if (selected) {
  const cases = ['tools', 'summary', 'summary-failure', 'stop', 'stop-summary', 'billing', 'mcp', 'mcp-stop', 'mcp-revoke', 'natural', 'ring'];
  for (const key of selected) assert.ok(cases.includes(key), `Unknown E2E case: ${key}`);
  if (selected.has('mcp-stop') || selected.has('mcp-revoke')) selected.add('mcp');
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function fixtureWorkspace() {
  assert.ok(existsSync(path.join(sourceRoot, 'node_modules/playwright-core')), 'Run npm ci first');
  const runs = path.join(fixtureBase, 'runs');
  await mkdir(runs, { recursive: true });
  const destination = await mkdtemp(path.join(runs, 'run-'));
  const excluded = new Set(['.data', '.runtime', '.git', 'node_modules', '__pycache__', '.vite', 'dist', 'dist-landing']);
  for (const item of ['backend', 'web', 'package.json', 'vite.config.ts', 'tsconfig.json', 'tests/fixtures/mcp_stdio_server.py']) {
    await mkdir(path.dirname(path.join(destination, item)), { recursive: true });
    await cp(path.join(sourceRoot, item), path.join(destination, item), { recursive: true,
      filter: value => !excluded.has(path.basename(value)) && !value.endsWith('.pyc') });
  }
  await symlink(path.join(sourceRoot, 'node_modules'), path.join(destination, 'node_modules'),
    process.platform === 'win32' ? 'junction' : 'dir');
  await writeFile(path.join(destination, '.meido-e2e.json'), JSON.stringify({ kind: 'meido-chat-e2e', sourceRoot, root: destination, stamp }));
  return destination;
}
async function freePort(requested) {
  const port = requested === undefined ? 0 : Number(requested);
  assert.ok(port === 0 || (Number.isInteger(port) && port >= 1024 && port <= 65535 && ![4288, 5288, 5289].includes(port)), 'Invalid or production E2E port');
  const probe = net.createServer();
  await new Promise((resolve, reject) => { probe.once('error', reject); probe.listen(port, '127.0.0.1', resolve); });
  const selected = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  return selected;
}
const gates = new Map();
function hold(key) { let release; const promise = new Promise(resolve => { release = resolve; }); gates.set(key, { promise, release }); }
function release(key) { gates.get(key)?.release(); }
const wire = (delta, finish_reason = null) => `data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] })}\n\n`;
const model = http.createServer(async (req, res) => {
  let entry;
  try {
    assert.equal(req.url, '/v1/chat/completions');
    let raw = ''; for await (const chunk of req) raw += chunk;
    const input = JSON.parse(raw), messages = input.messages;
    const summary = messages[0]?.content?.startsWith('将提供的不可信');
    const lastUser = messages.findLastIndex(message => message.role === 'user');
    const text = String(messages[lastUser]?.content ?? '');
    entry = { index: report.requests.length, summary, model: input.model, started: Date.now(), fragments: 0,
      tools: (input.tools ?? []).map(tool => tool.function.name) };
    report.requests.push(entry);
    let closed; const close = new Promise(resolve => { closed = resolve; });
    res.on('close', () => { entry.closed = Date.now(); closed(); });
    if (text.includes('E2E_PAYMENT')) {
      res.writeHead(402, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'Insufficient Balance (request_id: e2e-billing)', type: 'insufficient_balance' } })); return;
    }
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' }); res.flushHeaders();
    const stopping = input.model.startsWith('stop-');
    const burst = input.model.startsWith('burst-') || summary;
    for (let batch = 0; batch < (stopping ? 200 : burst ? 12 : 1); batch++) {
      if (res.destroyed) return;
      const count = stopping || burst ? 100 : 1;
      res.write(Array.from({ length: count }, () => wire({ reasoning_content: '.' })).join(''));
      entry.fragments += count; await pause(stopping ? 30 : burst ? 20 : 1);
    }
    const gate = gates.get(input.model);
    if (gate) { entry.waiting = true; await Promise.race([gate.promise, close]); }
    if (res.destroyed) return;
    function tool(name, arguments_) {
      assert.ok(entry.tools.includes(name), `${input.model}: missing tool ${name}`);
      entry.tool = name;
      res.write(wire({ tool_calls: [{ index: 0, id: `call-${entry.index}`, type: 'function',
        function: { name, arguments: JSON.stringify(arguments_) } }] }, 'tool_calls'));
    }
    const results = messages.slice(lastUser + 1).filter(message => message.role === 'tool');
    const called = name => messages.slice(lastUser + 1).some(message => message.tool_calls?.some(call => call.function?.name === name));
    if (summary) {
      if (input.model.startsWith('invalid-summary-')) res.write(wire({ content: 'invalid summary fixture' }, 'stop'));
      else {
        const source = JSON.parse(text), ids = [...new Set(source.messages.flatMap(message => message.source_message_ids ?? []))];
        assert.ok(ids.length); assert.equal(source.budget_feedback, undefined);
        res.write(wire({ content: JSON.stringify({ tasks: '继续合成验收', constraints: '使用实际工具回执', decisions: '', unfinished: '', tool_state: '', entities: '', source_message_ids: ids.slice(0, 2) }) }, 'stop'));
      }
    } else if (text.includes('E2E_TOOL') || text.includes('E2E_RESTORED')) {
      assert.ok(entry.tools.includes('list_dir'), 'Shiori core filesystem tool should be resident');
      if (text.includes('E2E_TOOL') && !called('tool_search')) tool('tool_search', { query: 'select:list_dir', description: '查询目录工具说明' });
      else if (!called('list_dir')) tool('list_dir', { path: '.', description: '读取隔离目录' });
      else { assert.match(results.at(-1).content, /Directory is empty|\[\.\]/); res.write(wire({ content: '真实目录工具验收完成。' }, 'stop')); }
    } else if (text.includes('E2E_MCP')) {
      assert.ok(!results.some(message => /MCP connection or discovery failed/.test(message.content)), 'Real MCP peer could not start');
      const name = mcpPrefix + (text.includes('SLOW') ? 'slow' : 'echo');
      if (text.includes('RESTORED')) assert.ok(entry.tools.includes(name), 'Next turn lost MCP tool');
      if (called(name)) {
        if (text.includes('REVOKE')) { assert.doesNotMatch(results.at(-1).content, /evidence browser/); res.write(wire({ content: '工具授权已撤回。' }, 'stop')); }
        else { assert.match(results.at(-1).content, /evidence browser/); res.write(wire({ content: '真实 MCP 工具验收完成。' }, 'stop')); }
      } else if (!entry.tools.some(value => value.startsWith(mcpPrefix)) && !called('mcp_add')) {
        if (!entry.tools.includes('mcp_add')) tool('tool_search', { query: 'select:mcp_add', description: '查找连接工具' });
        else tool('mcp_add', { server_id: mcpServer, description: '连接隔离工具服务' });
      } else if (!entry.tools.includes(name)) tool('tool_search', { query: `select:${name}`, description: '查找测试工具' });
      else if (!called(name)) tool(name, name.endsWith('echo') ? { text: 'evidence browser', description: '读取真实工具回执' } : { description: '等待测试工具响应' });
    } else if (input.model.startsWith('natural-')) {
      tool('finish_reply', { messages: ['拟人回复第一段。', '拟人回复第二段。'] });
    } else res.write(wire({ content: '流式验收完成。' }, 'stop'));
    res.end('data: [DONE]\n\n'); entry.completed = Date.now();
  } catch (error) { report.modelError = String(error.stack ?? error); if (!res.headersSent) res.writeHead(500); res.end(); }
});

async function until(check, message, timeout = 30000) {
  const end = Date.now() + timeout;
  for (;;) { assert.equal(report.modelError, undefined); const value = await check(); if (value) return value; assert.ok(Date.now() < end, message); await pause(50); }
}
async function json(endpoint, method = 'GET', body) {
  const response = await fetch(api + endpoint, { method, signal: AbortSignal.timeout(15000),
    headers: body ? { 'content-type': 'application/json' } : {}, body: body ? JSON.stringify(body) : undefined });
  assert.ok(response.ok, `${method} ${endpoint}: ${response.status} ${response.ok ? '' : await response.text()}`);
  return response.status === 204 ? null : response.json();
}
async function idleSession(roleId) {
  const response = await fetch(`${api}/api/roles/${roleId}/session`, { signal: AbortSignal.timeout(15000) });
  if (response.status === 503) {
    assert.equal(response.headers.get('Retry-After'), '1');
    assert.deepEqual(await response.json(), { detail: '回复仍在更新，正在重新同步' });
    return null;
  }
  assert.equal(response.status, 200, await response.clone().text());
  const session = await response.json(), sessionKey = `role:${roleId}`;
  assert.equal(session?.session?.roleId, roleId);
  assert.equal(session?.session?.sessionKey, sessionKey);
  assert.ok(Array.isArray(session.messages));
  for (const message of session.messages) assert.equal(message.sessionKey, sessionKey);
  assert.ok(session.activeRun === null || (session.activeRun && typeof session.activeRun === 'object'));
  if (session.activeRun !== null) {
    assert.equal(session.activeRun.roleId, roleId);
    assert.equal(session.activeRun.sessionKey, sessionKey);
    assert.ok(typeof session.activeRun.runId === 'string' && session.activeRun.runId.length > 0);
    assert.ok(['queued', 'created', 'running'].includes(session.activeRun.status));
    return null;
  }
  return session;
}
async function one(key, name, fn) {
  if (selected && !selected.has(key)) return;
  report.current = name; console.log(JSON.stringify({ case: name, state: 'running' }));
  const started = Date.now(), value = await fn();
  report.cases.push({ key, name, ms: Date.now() - started, ...value }); console.log(JSON.stringify({ case: name, state: 'passed' }));
}
let development, developmentEnded, browser, page, mcpConfiguration;
try {
  await mkdir(artifacts, { recursive: true });
  root = await fixtureWorkspace();
  report.fixtureRoot = root;
  const apiPort = await freePort(process.env.MEIDO_E2E_API_PORT);
  let webPort = await freePort(process.env.MEIDO_E2E_WEB_PORT);
  if (apiPort === webPort && process.env.MEIDO_E2E_WEB_PORT === undefined) webPort = await freePort();
  assert.notEqual(apiPort, webPort, 'API and web ports must differ');
  api = `http://127.0.0.1:${apiPort}`; web = `http://127.0.0.1:${webPort}`;
  report.ports = { apiPort, webPort };
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  const upstream = `http://127.0.0.1:${model.address().port}/v1`;
  const out = openSync(path.join(artifacts, 'dev-out.log'), 'w'), err = openSync(path.join(artifacts, 'dev-error.log'), 'w');
  development = spawn(process.execPath, [path.join(sourceRoot, 'scripts/local-runtime/start-dev.mjs'), '--code-root', root, '--shutdown-stdin'], {
    cwd: root, windowsHide: true, detached: process.platform !== 'win32', stdio: ['pipe', out, err], env: { ...process.env, HTTP_PROXY: '', HTTPS_PROXY: '', ALL_PROXY: '', NO_PROXY: '127.0.0.1,localhost',
      MEIDO_MEMORY_OPTIMIZER_ENABLED: 'false', MEIDO_DEV_API_PORT: String(apiPort), MEIDO_DEV_WEB_PORT: String(webPort) },
  });
  developmentEnded = new Promise(resolve => {
    development.once('exit', (code, signal) => resolve({ code, signal }));
    development.once('error', error => { report.developmentError = error.message; resolve({ code: null, error: error.message }); });
  });
  closeSync(out); closeSync(err); development.stdin.on('error', () => {});
  await until(async () => { assert.equal(report.developmentError, undefined); assert.equal(development.exitCode, null); try { return (await fetch(api + '/api/health', { signal: AbortSignal.timeout(1000) })).ok; } catch { return false; } }, 'Development startup', 60000);
  assert.deepEqual((await json('/api/roles')).roles, [], 'Refuse to seed an existing role collection');
  await json('/api/memory/settings', 'PUT', { enabled: false, embeddingEnabled: false });
  const settings = await json('/api/settings/advanced');
  await json('/api/settings/advanced', 'PUT', { expectedRevision: settings.revision, operationId: 'e2e-' + stamp, values: { ...settings.values, devMode: true } });
  async function role(label, capacity = 500000, natural = false) {
    const configuration = (await json('/api/model/configurations', 'POST', { providerId: 'custom', provider: 'fixture', baseUrl: upstream,
      model: label + '-' + stamp, api: 'openai-completions', apiKey: '', contextWindowTokens: capacity,
      modelAutoCompactTokenLimit: capacity === 120000 ? 75000 : capacity ? 400000 : null })).configuration;
    const r = (await json('/api/roles', 'POST', { name: label + '-' + stamp, profile: { profile: '本地合成浏览器验收。' }, agentConfig: { naturalChatEnabled: natural } })).role;
    await json('/api/roles/' + r.id + '/model-configuration', 'PUT', { configurationId: configuration.id });
    return { ...r, configuration };
  }
  const roles = { normal: await role('burst-normal'), long: await role('summary', 120000), invalid: await role('invalid-summary', 120000),
    stop: await role('stop-normal'), stopSummary: await role('stop-summary', 120000), idle: await role('idle'),
    billing: await role('billing'), mcp: await role('mcp'), natural: await role('natural', 500000, true), unknown: await role('unknown', null) };
  report.roles = Object.fromEntries(Object.entries(roles).map(([key, r]) => [key, r.id]));
  execFileSync(python, [path.join(directory, 'seed-history.py'), root, roles.normal.id, roles.long.id, roles.invalid.id, roles.stop.id, roles.stopSummary.id], { cwd: root, windowsHide: true });
  browser = await chromium.launch({ ...(process.env.MEIDO_E2E_BROWSER ? { executablePath: process.env.MEIDO_E2E_BROWSER } : {}), headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
  await context.route('**/*', route => [api, web].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
  page = await context.newPage(); page.on('pageerror', error => report.browserErrors.push(error.message));
  await page.goto(web + '/app');
  async function select(r) {
    const list = page.locator('aside[aria-label="对话列表"]');
    await list.waitFor({ state: 'attached' });
    if (!(await list.isVisible())) await page.getByRole('button', { name: '对话列表', exact: true }).click();
    await page.locator('aside[aria-label="对话列表"] button').filter({ has: page.getByText(r.name, { exact: true }) }).click();
    await page.getByRole('heading', { name: r.name, exact: true }).waitFor();
  }
  async function send(text) { await page.getByRole('textbox', { name: '消息内容', exact: true }).fill(text); await page.getByRole('button', { name: '发送', exact: true }).click(); }
  async function done() { await until(async () => (await page.getByRole('button', { name: '发送', exact: true }).count()) > 0, 'Reply did not finish', 60000); }
  async function pagesDuringStream() {
    const started = Date.now(), response = page.waitForResponse(value => value.url().endsWith('/api/plugin-management') && value.status() === 200);
    await page.getByRole('button', { name: '设置', exact: true }).click(); await page.getByRole('button', { name: '插件', exact: true }).click(); await response;
    await page.locator('.plugin-list').getByText('NovelAI 生图', { exact: true }).waitFor();
    assert.equal(await page.getByText('状态读取失败', { exact: true }).count(), 0);
    const ms = Date.now() - started; assert.ok(ms < 5000, `Settings blocked for ${ms}ms`);
    await page.getByRole('dialog', { name: '设置', exact: true }).getByRole('button', { name: '关闭', exact: true }).click(); return ms;
  }
  await one('tools', '长历史工具往返、切页、跨角色与刷新恢复', async () => {
    await select(roles.normal); hold(roles.normal.configuration.model);
    const ring = page.getByRole('button', { name: /^上下文用量/ });
    await until(async () => /500,000/.test(await ring.getAttribute('aria-label')), 'Saved long context usage');
    const before = report.requests.length; await send('E2E_TOOL 查看隔离目录');
    await until(() => report.requests[before]?.waiting, 'Streaming gate');
    assert.equal(await ring.getAttribute('aria-busy'), 'true'); assert.match(await ring.getAttribute('aria-label'), /500,000/);
    const settingsMs = await pagesDuringStream();
    await select(roles.idle); assert.equal(await page.getByRole('button', { name: '停止回复', exact: true }).count(), 0);
    assert.equal(await page.getByText('.'.repeat(1200), { exact: true }).count(), 0);
    await select(roles.normal); await page.reload(); await select(roles.normal);
    await page.getByText('.'.repeat(1200), { exact: true }).waitFor({ timeout: 20000 });
    assert.equal(report.requests.length, before + 1, 'Refresh replayed the accepted input');
    assert.equal(await page.getByText('准备上下文', { exact: true }).count(), 0);
    release(roles.normal.configuration.model); await done();
    assert.equal(report.requests.length - before, 3);
    await page.locator('.message.assistant .reply-content').filter({ hasText: '真实目录工具验收完成。' }).waitFor();
    await page.screenshot({ path: path.join(artifacts, 'tools-and-ring.png') });
    const next = report.requests.length; await send('E2E_RESTORED 再使用已加载工具'); await done();
    assert.equal(report.requests.length - next, 2); assert.equal(report.requests[next].tool, 'list_dir');
    return { settingsMs, firstTurnRequests: 3, nextTurnRequests: 2 };
  });
  await one('summary', '摘要成功提交且后续对话复用', async () => {
    await select(roles.long); const before = report.requests.length; await send('E2E_LONG 继续历史');
    await until(() => report.requests.slice(before).some(r => r.summary), 'Summary not requested'); const settingsMs = await pagesDuringStream(); await done();
    assert.equal(report.requests.slice(before).filter(r => r.summary).length, 1);
    const diagnostics = (await json('/api/settings/advanced/diagnostics')).diagnostics.find(d => d.roleId === roles.long.id)?.compaction;
    assert.equal(diagnostics.stage, 'committed'); const next = report.requests.length;
    await send('E2E_NEXT 继续摘要后的会话'); await done();
    assert.equal(report.requests.slice(next).filter(r => r.summary).length, 0);
    const session = await json(`/api/roles/${roles.long.id}/session`); assert.ok(session.messages.length >= 164);
    return { settingsMs, savedOriginalMessages: session.messages.length };
  });
  await one('summary-failure', '摘要失败保留原文且回退后可继续', async () => {
    await select(roles.invalid); const before = report.requests.length; await send('E2E_LONG 摘要失败验收'); await done();
    const calls = report.requests.slice(before); assert.equal(calls.filter(r => r.summary).length, 1); assert.ok(calls.some(r => !r.summary));
    const diagnostics = (await json('/api/settings/advanced/diagnostics')).diagnostics.find(d => d.roleId === roles.invalid.id)?.compaction;
    assert.notEqual(diagnostics.stage, 'committed');
    const session = await json(`/api/roles/${roles.invalid.id}/session`); assert.ok(session.messages.length >= 162);
    await send('E2E_NEXT 失败回退后继续'); await done();
    return { stage: diagnostics.stage, savedOriginalMessages: session.messages.length };
  });
  for (const [key, r, summary] of [['stop', roles.stop, false], ['stop-summary', roles.stopSummary, true]]) await one(key, summary ? '摘要停止不提交窗口' : '普通流停止保存部分内容', async () => {
    await select(r); const before = report.requests.length; await send('E2E_STOP');
    await until(() => report.requests[before]?.fragments >= 100, 'Stop stream not started'); assert.equal(report.requests[before].summary, summary);
    const started = Date.now(); await page.getByRole('button', { name: '停止回复', exact: true }).click(); await done(); const stopMs = Date.now() - started;
    assert.ok(stopMs < 5000); await until(() => report.requests[before].closed, 'Cancelled provider connection');
    assert.equal(report.requests[before].completed, undefined);
    if (summary) assert.notEqual((await json('/api/settings/advanced/diagnostics')).diagnostics.find(d => d.roleId === r.id)?.compaction?.stage, 'committed');
    else { const partial = (await json(`/api/roles/${r.id}/session`)).messages.filter(m => m.role === 'assistant').at(-1);
      assert.equal(partial.status, 'failed'); assert.ok(partial.metadata.process.some(p => p.type === 'thinking' && p.text.length > 0)); }
    return { stopMs };
  });
  await one('billing', '供应商 402 正确收尾且下一条能发送', async () => {
    await select(roles.billing); await send('E2E_PAYMENT'); await done(); await page.getByText(/Insufficient Balance/).first().waitFor();
    assert.equal((await json(`/api/roles/${roles.billing.id}/session`)).activeRun, null);
    await send('E2E_RECOVER 继续正常对话'); await done(); await page.locator('.message.assistant .reply-content').filter({ hasText: '流式验收完成。' }).waitFor();
  });
  await one('mcp', '真实 MCP 发现、调用与下一轮工具恢复', async () => {
    const peer = path.join(root, '.data/development/e2e-peer-' + stamp); await mkdir(peer, { recursive: true });
    const executable = execFileSync(python, ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8', windowsHide: true }).trim();
    mcpConfiguration = { serverId: mcpServer, name: 'E2E stdio', command: [executable, path.join(root, 'tests/fixtures/mcp_stdio_server.py'), peer], cwd: peer, env: {}, roleIds: [roles.mcp.id], risk: 'external', timeoutSeconds: 60 };
    await json('/api/mcp/servers', 'POST', mcpConfiguration);
    await select(roles.mcp); await send('E2E_MCP 调用真实工具'); await done();
    const business = () => readFileSync(path.join(peer, 'business.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(business().length, 1); assert.equal(business()[0].value, 'evidence browser');
    const before = report.requests.length; await send('E2E_MCP_RESTORED 使用已加载 MCP'); await done();
    assert.equal(business().length, 2); assert.equal(report.requests[before].tool, mcpPrefix + 'echo');
    report.peer = peer;
  });
  await one('mcp-stop', '慢 MCP 工具刷新续接及停止清理', async () => {
    assert.ok(report.peer, 'Run mcp before mcp-stop'); await select(roles.mcp); await send('E2E_MCP_SLOW');
    await until(() => existsSync(path.join(report.peer, 'started')), 'MCP business tool was not invoked');
    const requests = report.requests.length; await page.reload(); await select(roles.mcp); const settingsMs = await pagesDuringStream();
    assert.equal(report.requests.length, requests, 'Refresh re-executed slow tool');
    const started = Date.now(); await page.getByRole('button', { name: '停止回复', exact: true }).click(); await done();
    assert.ok(!existsSync(path.join(report.peer, 'effect.txt')), 'Cancelled tool performed late effect');
    const events = readFileSync(path.join(report.peer, 'events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
    assert.equal(events.filter(e => e.method === 'tools/call' && e.params.name === 'slow').length, 1);
    for (const name of ['pid', 'child_pid']) { const pid = Number(readFileSync(path.join(report.peer, name), 'utf8'));
      await until(() => { try { process.kill(pid, 0); return false; } catch { return true; } }, 'MCP child still alive', 5000); }
    return { settingsMs, stopMs: Date.now() - started };
  });
  await one('mcp-revoke', '模型等待时撤销工具仍阻止真实调用', async () => {
    assert.ok(mcpConfiguration, 'Run mcp before mcp-revoke');
    await select(roles.mcp); hold(roles.mcp.configuration.model);
    const before = report.requests.length, businessBefore = readFileSync(path.join(report.peer, 'business.jsonl'), 'utf8');
    await send('E2E_MCP_REVOKE'); await until(() => report.requests[before]?.waiting, 'MCP revoke gate');
    assert.ok(report.requests[before].tools.includes(mcpPrefix + 'echo'));
    await json('/api/mcp/servers/' + mcpServer, 'PUT', { ...mcpConfiguration, enabled: false });
    release(roles.mcp.configuration.model); await done();
    assert.equal(readFileSync(path.join(report.peer, 'business.jsonl'), 'utf8'), businessBefore, 'Revoked tool was executed');
    assert.equal((await json(`/api/roles/${roles.mcp.id}/session`)).activeRun, null);
    await send('E2E_RECOVER 撤销工具后继续聊天'); await done();
  });
  await one('natural', '拟人分段结束后可以继续发送', async () => {
    const before = report.requests.length;
    await select(roles.natural); await send('E2E_NATURAL');
    await page.locator('.message.assistant .reply-content').filter({ hasText: '拟人回复第一段。' }).waitFor();
    await page.locator('.message.assistant .reply-content').filter({ hasText: '拟人回复第二段。' }).waitFor();
    const session = await until(() => idleSession(roles.natural.id), 'Natural run cleanup');
    assert.equal(session.messages.filter(m => m.role === 'assistant' && m.content.includes('拟人回复')).length, 2);
    await send('E2E_NATURAL_NEXT');
    const next = await until(async () => { const value = await idleSession(roles.natural.id);
      return value && value.messages.filter(m => m.role === 'assistant' && m.content.includes('拟人回复')).length === 4 ? value : null;
    }, 'Next natural reply did not finish');
    assert.equal(next.messages.filter(m => m.role === 'assistant' && m.content.includes('拟人回复')).length, 4);
    assert.equal(report.requests.length - before, 2, 'Natural reply recovery repeated a model request');
    return { modelRequests: report.requests.length - before };
  });
  await one('ring', 'Shiori 圆环、未知容量与移动端触摸', async () => {
    await select(roles.idle); const ring = page.getByRole('button', { name: /^上下文用量/ });
    await until(async () => /500,000/.test(await ring.getAttribute('aria-label')), 'Idle context status');
    const svg = await ring.locator('svg').boundingBox(); assert.equal(svg.width, 20); assert.equal(svg.height, 20);
    await ring.hover(); await page.getByRole('tooltip').waitFor(); assert.equal(await page.getByRole('tooltip').locator(':scope > div').count(), 2);
    assert.equal(await page.getByRole('button', { name: '重新检查', exact: true }).count(), 0);
    await page.keyboard.press('Escape'); await select(roles.unknown);
    await until(async () => /模型尚未配置上下文容量/.test(await ring.getAttribute('aria-label')), 'Completed unknown-capacity inspection');
    assert.equal(await ring.locator('svg circle').count(), 1);
    const mobile = await browser.newContext({ viewport: { width: 360, height: 800 }, hasTouch: true, isMobile: true });
    await mobile.route('**/*', route => [api, web].includes(new URL(route.request().url()).origin) ? route.continue() : route.abort());
    page = await mobile.newPage(); page.on('pageerror', error => report.browserErrors.push(error.message));
    await page.goto(web + '/app'); await select(roles.unknown);
    await page.getByRole('button', { name: /^上下文用量/ }).tap(); await page.getByRole('tooltip').waitFor();
    await until(async () => /模型尚未配置上下文容量/.test(await page.getByRole('button', { name: /^上下文用量/ }).getAttribute('aria-label')), 'Completed mobile unknown-capacity inspection');
    assert.equal(await page.getByRole('button', { name: /^上下文用量/ }).locator('svg circle').count(), 1);
    const box = await page.getByRole('tooltip').boundingBox(); assert.ok(box.x >= 0 && box.x + box.width <= 360);
    await page.screenshot({ path: path.join(artifacts, 'ring-mobile.png') });
  });
  assert.equal(report.modelError, undefined); assert.deepEqual(report.browserErrors, []); report.ok = true;
} catch (error) {
  report.ok = false; report.error = String(error.stack ?? error); if (page) await page.screenshot({ path: path.join(artifacts, 'failure.png') }).catch(() => {}); process.exitCode = 1;
} finally {
  for (const gate of gates.values()) gate.release();
  const cleanupErrors = [];
  try { if (browser) await browser.close(); } catch (error) { cleanupErrors.push(String(error)); }
  if (development) {
    let timer;
    try {
      if (development.exitCode === null && development.signalCode === null) development.stdin.end('shutdown\n');
      const exited = await Promise.race([developmentEnded, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Owned development launcher did not stop within 20s')), 20000);
      })]);
      assert.equal(exited.code, 0, `Development exit: ${JSON.stringify(exited)}`);
    } catch (error) {
      cleanupErrors.push(String(error));
      // Last-resort cleanup never turns a failed graceful shutdown into success.
      // Both paths target only the process/group created by this test.
      if (development.pid && development.exitCode === null && development.signalCode === null) {
        try {
          if (process.platform === 'win32') execFileSync('taskkill', ['/PID', String(development.pid), '/T', '/F'], { windowsHide: true, timeout: 10000 });
          else process.kill(-development.pid, 'SIGKILL');
          report.forcedDevelopmentStop = true;
        } catch (killError) { cleanupErrors.push(String(killError)); }
      }
    } finally { clearTimeout(timer); }
  }
  model.closeAllConnections();
  if (model.listening) await new Promise(resolve => model.close(resolve));
  if (cleanupErrors.length) { report.cleanupErrors = cleanupErrors; report.ok = false; process.exitCode = 1; }
  report.developmentExit = development?.exitCode; report.finishedAt = new Date().toISOString();
  await writeFile(path.join(artifacts, `report-${stamp}.json`), JSON.stringify(report, null, 2));
  await writeFile(path.join(artifacts, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ ok: report.ok, cases: report.cases, error: report.error, cleanupErrors: report.cleanupErrors,
    modelRequests: report.requests.length, developmentExit: report.developmentExit, artifacts }));
}
