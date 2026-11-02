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
const report = { cases: [], requests: [], auxiliaryRequests: [], browserErrors: [], startedAt: new Date().toISOString() };
let failEmbeddings = false;
const only = process.env.MEIDO_E2E_ONLY;
const selected = only ? new Set(only.split(',')) : null;
if (selected) {
  const cases = ['tools', 'summary', 'summary-failure', 'stop', 'stop-summary', 'billing', 'mcp', 'mcp-stop', 'mcp-revoke', 'natural', 'ring', 'memory', 'optimizer'];
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
    let raw = ''; for await (const chunk of req) raw += chunk;
    const input = JSON.parse(raw);
    if (req.url === '/v1/embeddings') {
      const embedding = { kind: 'embedding', started: Date.now(), model: input.model, failed: failEmbeddings };
      report.auxiliaryRequests.push(embedding);
      const gate = gates.get('embedding');
      if (gate) { embedding.waiting = true; await gate.promise; }
      res.writeHead(failEmbeddings ? 503 : 200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(failEmbeddings ? { error: { message: 'controlled embedding outage' } } :
        { data: [{ embedding: [1, ...Array((input.dimensions || 64) - 1).fill(0)] }] }));
      embedding.completed = Date.now(); return;
    }
    assert.equal(req.url, '/v1/chat/completions');
    const messages = input.messages;
    const summary = messages[0]?.content?.startsWith('将提供的不可信');
    const lastUser = messages.findLastIndex(message => message.role === 'user');
    const text = String(messages[lastUser]?.content ?? '');
    const maxTokens = input.max_tokens ?? input.max_completion_tokens;
    const kind = summary ? '' : ({ 16384: 'optimizer', 2048: 'self', 1024: 'markdown', 512: 'recent', 600: 'implicit', 96: 'check', 80: 'hyde', 128: 'tag' })[maxTokens];
    if (kind) {
      entry = { kind, model: input.model, started: Date.now() }; report.auxiliaryRequests.push(entry);
      let closed; const close = new Promise(resolve => { closed = resolve; });
      res.on('close', () => { entry.closed = Date.now(); closed(); });
      const gate = gates.get(`${kind}:${input.model}`);
      if (gate) { entry.waiting = true; await Promise.race([gate.promise, close]); }
      if (res.destroyed) return;
      const content = kind === 'markdown' ? JSON.stringify({ history_entries: [{ summary: '[2026-11-02 12:00] MEMORY_E2E 你喜欢茶，我们讨论了旅行。', emotional_weight: 3 }], pending_items: [{ tag: 'preference', content: 'MEMORY_E2E 你喜欢茶' }] })
        : kind === 'recent' ? JSON.stringify({ ongoing_threads: ['MEMORY_E2E 我们在讨论旅行'], active_topics: ['茶与旅行'] })
        : kind === 'optimizer' ? '# 我的长期记忆\n\n## 关于你\n- OPTIMIZER_COMMITTED\n'
        : kind === 'self' ? '# 我是谁\n\n## 我的性格与形象\n- OPTIMIZER_SELF_COMMITTED\n'
        : kind === 'implicit' ? JSON.stringify({ profile: [], preference: [], procedure: [] }) : kind === 'hyde' ? '你喜欢茶和旅行' : '[]';
      if (input.stream) { res.writeHead(200, { 'content-type': 'text/event-stream' }); res.end(wire({ content }, 'stop') + 'data: [DONE]\n\n'); }
      else { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ choices: [{ message: { content }, finish_reason: 'stop' }] })); }
      entry.completed = Date.now(); return;
    }
    entry = { index: report.requests.length, summary, model: input.model, started: Date.now(), fragments: 0,
      tools: (input.tools ?? []).map(tool => tool.function.name), messages };
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
function launchDevelopment(apiPort, webPort) {
  const out = openSync(path.join(artifacts, 'dev-out.log'), 'a'), err = openSync(path.join(artifacts, 'dev-error.log'), 'a');
  development = spawn(process.execPath, [path.join(sourceRoot, 'scripts/local-runtime/start-dev.mjs'), '--code-root', root, '--shutdown-stdin'], {
    cwd: root, windowsHide: true, detached: process.platform !== 'win32', stdio: ['pipe', out, err], env: { ...process.env, HTTP_PROXY: '', HTTPS_PROXY: '', ALL_PROXY: '', NO_PROXY: '127.0.0.1,localhost',
      MEIDO_MEMORY_OPTIMIZER_ENABLED: 'false', MEIDO_DEV_API_PORT: String(apiPort), MEIDO_DEV_WEB_PORT: String(webPort) },
  });
  developmentEnded = new Promise(resolve => {
    development.once('exit', (code, signal) => resolve({ code, signal }));
    development.once('error', error => { report.developmentError = error.message; resolve({ code: null, error: error.message }); });
  });
  closeSync(out); closeSync(err); development.stdin.on('error', () => {});
}
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
  launchDevelopment(apiPort, webPort);
  await until(async () => { assert.equal(report.developmentError, undefined); assert.equal(development.exitCode, null); try { return (await fetch(api + '/api/health', { signal: AbortSignal.timeout(1000) })).ok; } catch { return false; } }, 'Development startup', 60000);
  assert.deepEqual((await json('/api/roles')).roles, [], 'Refuse to seed an existing role collection');
  await json('/api/memory/settings', 'PUT', { enabled: false, embeddingEnabled: false });
  const settings = await json('/api/settings/advanced');
  await json('/api/settings/advanced', 'PUT', { expectedRevision: settings.revision, operationId: 'e2e-' + stamp, values: { ...settings.values, devMode: true } });
  async function role(label, capacity = 500000, natural = false, trigger) {
    const configuration = (await json('/api/model/configurations', 'POST', { providerId: 'custom', provider: 'fixture', baseUrl: upstream,
      model: label + '-' + stamp, api: 'openai-completions', apiKey: '', contextWindowTokens: capacity,
      modelAutoCompactTokenLimit: trigger ?? (capacity === 120000 ? 75000 : capacity ? 400000 : null) })).configuration;
    const r = (await json('/api/roles', 'POST', { name: label + '-' + stamp, profile: { profile: '本地合成浏览器验收。' }, agentConfig: { naturalChatEnabled: natural } })).role;
    await json('/api/roles/' + r.id + '/model-configuration', 'PUT', { configurationId: configuration.id });
    return { ...r, configuration };
  }
  const roles = { normal: await role('burst-normal'), long: await role('summary', 120000), invalid: await role('invalid-summary', 64000, false, 35000), soft: await role('invalid-summary-soft', 180000, false, 75000),
    stop: await role('stop-normal'), stopSummary: await role('stop-summary', 120000), idle: await role('idle'),
    billing: await role('billing'), mcp: await role('mcp'), natural: await role('natural', 500000, true), unknown: await role('unknown', null) };
  report.roles = Object.fromEntries(Object.entries(roles).map(([key, r]) => [key, r.id]));
  execFileSync(python, [path.join(directory, 'seed-history.py'), root, roles.normal.id, roles.long.id, roles.invalid.id, roles.soft.id, roles.stop.id, roles.stopSummary.id], { cwd: root, windowsHide: true });
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
    const ring = page.getByRole('button', { name: /^压缩上下文/ });
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
  await one('summary-failure', '软阈值失败继续原文，硬阈值失败停止且保留原文', async () => {
    await select(roles.invalid); const before = report.requests.length; await send('E2E_LONG 摘要失败验收'); await done();
    const calls = report.requests.slice(before); assert.equal(calls.filter(r => r.summary).length, 1); assert.equal(calls.filter(r => !r.summary).length, 0);
    const diagnostics = (await json('/api/settings/advanced/diagnostics')).diagnostics.find(d => d.roleId === roles.invalid.id)?.compaction;
    assert.notEqual(diagnostics.stage, 'committed');
    const session = await json(`/api/roles/${roles.invalid.id}/session`); assert.ok(session.messages.length >= 162);
    await select(roles.soft); const softBefore = report.requests.length;
    await send('E2E_LONG 软阈值摘要失败验收'); await done();
    assert.equal(report.requests.slice(softBefore).filter(r => r.summary).length, 1);
    assert.equal(report.requests.slice(softBefore).filter(r => !r.summary).length, 1);
    assert.ok((await json(`/api/roles/${roles.soft.id}/session`)).messages.length >= 162);
    return { hardStage: diagnostics.stage, hardNormalRequests: 0, softNormalRequests: 1, savedOriginalMessages: session.messages.length };
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
  await one('memory', '记忆开启：覆盖提交、消费者故障、重启复用、工具、停止与手动压缩', async () => {
    const fixture = (r, action = 'inspect') => JSON.parse(execFileSync(python,
      [path.join(directory, 'memory-fixture.py'), root, action, r.id], { cwd: root, windowsHide: true, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }));
    await json('/api/memory/embedding/configuration', 'PUT', { provider: 'local', baseUrl: upstream, model: 'memory-fixture', dimensions: 64, apiKey: 'local-fixture' });
    await json('/api/memory/settings', 'PUT', { enabled: true, embeddingEnabled: true });
    const auto = await role('memory-auto', 120000), manual = await role('memory-manual'), stopping = await role('memory-stop', 120000), unavailable = await role('memory-unavailable');
    execFileSync(python, [path.join(directory, 'seed-history.py'), root, auto.id], { cwd: root, windowsHide: true });
    fixture(manual, 'seed'); fixture(unavailable, 'seed'); fixture(stopping, 'seed-stop');
    await page.reload(); await select(auto);
    hold('markdown:' + auto.configuration.model); failEmbeddings = true;
    const before = report.requests.length;
    await send('E2E_TOOL 开启记忆后使用真实工具');
    await until(() => report.auxiliaryRequests.some(r => r.kind === 'markdown' && r.model === auto.configuration.model && r.waiting), 'Markdown generation gate');
    const settingsMs = await pagesDuringStream();
    await page.reload(); await select(auto);
    assert.equal(report.requests.length, before, 'Refresh submitted a normal turn during Markdown preparation');
    release('markdown:' + auto.configuration.model); await done();
    await until(() => fixture(auto).progress.pendingEvents?.length > 0, 'Failed semantic consumer was not preserved');
    const covered = fixture(auto);
    assert.ok(covered.progress.lastSequence > 0 && covered.windows.length === 1);
    assert.match(covered.documents['HISTORY.md'], /MEMORY_E2E/);
    assert.equal(report.requests.slice(before).filter(r => !r.summary).length, 3, 'Tool chain repeated after coverage');
    assert.equal(report.requests.slice(before).filter(r => r.summary).length, 1);
    const next = report.requests.length; await send('E2E_RESTORED 继续使用真实工具'); await done();
    assert.equal(report.requests.slice(next).filter(r => !r.summary).length, 2);
    assert.equal(report.requests.slice(next).filter(r => r.summary).length, 0);
    failEmbeddings = false;
    const history = covered.documents['HISTORY.md'], version = covered.progress.memoryVersion;
    const drafts = report.auxiliaryRequests.filter(r => r.kind === 'markdown' && r.model === auto.configuration.model).length;
    development.stdin.end('shutdown\n');
    const exit = await developmentEnded; assert.equal(exit.code, 0);
    launchDevelopment(apiPort, webPort);
    await until(async () => { try { return (await fetch(api + '/api/health')).ok; } catch { return false; } }, 'Memory restart', 60000);
    await until(() => !fixture(auto).progress.pendingEvents?.length, 'Pending semantic consumer did not resume');
    assert.equal(fixture(auto).documents['HISTORY.md'], history);
    assert.equal(fixture(auto).progress.memoryVersion, version);
    assert.equal(report.auxiliaryRequests.filter(r => r.kind === 'markdown' && r.model === auto.configuration.model).length, drafts);
    await page.reload(); await select(auto); const restarted = report.requests.length;
    hold('embedding'); const embeddingStarted = Date.now();
    await send('E2E_RESTORED 重启后继续');
    await until(() => report.auxiliaryRequests.some(r => r.kind === 'embedding' && r.waiting && r.started >= embeddingStarted), 'Slow query embedding gate');
    const embeddingSettingsMs = await pagesDuringStream();
    assert.equal(report.requests.length, restarted, 'An ordinary request bypassed the pending embedding boundary');
    await page.reload(); await select(auto);
    release('embedding'); await done();
    assert.equal(report.requests.slice(restarted).filter(r => r.summary).length, 0);
    assert.equal(report.requests.slice(restarted).filter(r => !r.summary).length, 2);
    assert.ok(fixture(auto).records > 0);

    await select(stopping); hold('markdown:' + stopping.configuration.model);
    await send('E2E_STOP_MARKDOWN');
    await until(() => report.auxiliaryRequests.some(r => r.kind === 'markdown' && r.model === stopping.configuration.model && r.waiting), 'Stop Markdown gate');
    await until(async () => {
      const session = await json(`/api/roles/${stopping.id}/session`);
      return session.activeRun?.phase === 'summarizing';
    }, 'Foreground did not reach memory coverage while Markdown was pending');
    const stopStarted = Date.now(); await page.getByRole('button', { name: '停止回复', exact: true }).click(); await done();
    const stopMs = Date.now() - stopStarted; assert.ok(stopMs < 3000, `Markdown Stop blocked for ${stopMs}ms`);
    await until(() => report.auxiliaryRequests.some(r => r.kind === 'markdown' && r.model === stopping.configuration.model && r.closed), 'Cancelled Markdown HTTP connection remained open', 3000);
    release('markdown:' + stopping.configuration.model);
    await pause(300);
    assert.equal(fixture(stopping).progress.lastSequence ?? 0, 0);
    assert.equal(fixture(stopping).windows.length, 0);

    await select(manual); const count = fixture(manual).messages;
    const ring = page.getByRole('button', { name: /^压缩上下文/ });
    await until(async () => (await ring.getAttribute('aria-disabled')) === 'false', 'Manual eligible prefix below threshold');
    await page.getByRole('textbox', { name: '消息内容', exact: true }).fill('保留未发送草稿');
    const compacted = page.waitForResponse(r => r.url().endsWith('/context-compact') && r.status() === 200);
    await ring.click(); assert.equal((await (await compacted).json()).compacted, true);
    assert.equal(fixture(manual).messages, count);
    assert.equal(await page.getByRole('textbox', { name: '消息内容', exact: true }).inputValue(), '保留未发送草稿');
    await send('/compact'); await until(async () => (await ring.getAttribute('aria-busy')) === 'false', 'Slash compact completion');
    assert.equal(fixture(manual).messages, count);
    assert.equal(await page.getByRole('textbox', { name: '消息内容', exact: true }).inputValue(), '/compact');

    await json('/api/memory/settings', 'PUT', { enabled: false, embeddingEnabled: false });
    await select(unavailable); const unavailableBefore = report.requests.length;
    await send('E2E_HOST_DOCS 仍使用宿主文档'); await done();
    const request = report.requests.slice(unavailableBefore).find(r => !r.summary);
    assert.match(JSON.stringify(request.messages[0]), /HOST_MEMORY_E2E/);
    assert.ok(!request.tools.includes('memorize') && !request.tools.includes('recall_memory'));
    await select(roles.idle); const isolatedBefore = report.requests.length;
    await send('E2E_SCOPE 另一角色'); await done();
    assert.doesNotMatch(JSON.stringify(report.requests[isolatedBefore].messages), /HOST_MEMORY_E2E|MEMORY_E2E/);
    await page.screenshot({ path: path.join(artifacts, 'memory-scope-isolation.png') });
    return { settingsMs, embeddingSettingsMs, stopMs, coverageVersion: version, manualSavedMessages: count, restartReused: true,
      consumerRecovered: true, toolRequests: [3, 2, 2], semanticRecords: fixture(auto).records };
  });
  await one('ring', 'Shiori 圆环、未知容量与移动端触摸', async () => {
    await select(roles.idle); const ring = page.getByRole('button', { name: /^压缩上下文/ });
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
    // aria-disabled prevents compaction; a physical touch still opens usage.
    await page.getByRole('button', { name: /^压缩上下文/ }).tap({ force: true }); await page.getByRole('tooltip').waitFor();
    await until(async () => /模型尚未配置上下文容量/.test(await page.getByRole('button', { name: /^压缩上下文/ }).getAttribute('aria-label')), 'Completed mobile unknown-capacity inspection');
    assert.equal(await page.getByRole('button', { name: /^压缩上下文/ }).locator('svg circle').count(), 1);
    const box = await page.getByRole('tooltip').boundingBox(); assert.ok(box.x >= 0 && box.x + box.width <= 360);
    await page.screenshot({ path: path.join(artifacts, 'ring-mobile.png') });
  });
  await one('optimizer', '优化器开启：恢复旧快照、停用取消请求、并发候选保留与重新提交', async () => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const fixture = (r, action = 'inspect') => JSON.parse(execFileSync(python,
      [path.join(directory, 'memory-fixture.py'), root, action, r.id], { cwd: root, windowsHide: true, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }));
    // All preceding scenarios are complete. Use the real deletion API inside
    // this owned disposable workspace so the serial upstream optimizer has one
    // eligible role, rather than shortening its production inter-step delay.
    for (const existing of (await json('/api/roles')).roles) await json('/api/roles/' + existing.id, 'DELETE');
    const target = await role('optimizer'); fixture(target, 'optimizer-seed');
    const policy = async enabled => {
      const settings = await json('/api/settings/advanced');
      await json('/api/settings/advanced', 'PUT', { expectedRevision: settings.revision,
        operationId: 'optimizer-' + Date.now(), values: { ...settings.values, memoryOptimizerEnabled: enabled, memoryOptimizerIntervalSeconds: 60 } });
    };
    await json('/api/memory/settings', 'PUT', { enabled: false, embeddingEnabled: false });
    hold('optimizer:' + target.configuration.model);
    await policy(true); await page.reload(); await select(target);
    const waiting = () => report.auxiliaryRequests.filter(r => r.kind === 'optimizer' && r.model === target.configuration.model && r.waiting);
    await until(() => waiting().length === 1, 'Enabled periodic optimizer did not start', 75000);
    assert.equal(fixture(target).snapshot, true);
    const status = await json(`/api/roles/${target.id}/context-status`);
    assert.notEqual(status.reason, 'busy', 'Independent optimizer blocked context inspection');
    const settingsMs = await pagesDuringStream();
    fixture(target, 'optimizer-append');
    await policy(false);
    await until(() => waiting()[0].closed, 'Disabled optimizer HTTP remained open', 3000);
    const restored = fixture(target);
    assert.equal(restored.snapshot, false);
    for (const marker of ['CANDIDATE_INITIAL', 'CANDIDATE_RECOVERED', 'CANDIDATE_CONCURRENT']) assert.match(restored.documents['PENDING.md'], new RegExp(marker));
    assert.equal(restored.documents['MEMORY.md'], 'OPTIMIZER_ORIGINAL_MEMORY');
    assert.equal(restored.documents['SELF.md'], 'OPTIMIZER_ORIGINAL_SELF');
    release('optimizer:' + target.configuration.model); hold('optimizer:' + target.configuration.model);
    await policy(true);
    await until(() => waiting().length === 2, 'Re-enabled optimizer did not start a new generation', 75000);
    release('optimizer:' + target.configuration.model);
    await until(() => fixture(target).documents['SELF.md'].includes('OPTIMIZER_SELF_COMMITTED'), 'Optimizer SELF did not commit', 25000);
    const committed = fixture(target); assert.match(committed.documents['MEMORY.md'], /OPTIMIZER_COMMITTED/);
    assert.equal(committed.snapshot, false);
    for (const marker of ['CANDIDATE_INITIAL', 'CANDIDATE_RECOVERED', 'CANDIDATE_CONCURRENT']) {
      assert.match(committed.documents['HISTORY.md'], new RegExp(marker));
      assert.doesNotMatch(committed.documents['PENDING.md'], new RegExp(marker));
    }
    await policy(false);
    await page.screenshot({ path: path.join(artifacts, 'optimizer-enabled.png') });
    return { settingsMs, cancelledHttp: true, pendingRestored: true, generations: waiting().length, engineDisabled: true };
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
