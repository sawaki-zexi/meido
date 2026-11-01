import { fakeBackend, sse, type Handler, type Reply } from './fakeBackend';
import type { ChatRunSnapshot } from '../api/stream';
import type { Message } from '../api/types';
import { applyStreamEvent } from '../features/chat/useChat';
import { parseStreamEvent } from '../api/stream';

/** Presentation fixtures describe a CHAT script; expose it through the real
 * submit + read + subscribe routes. Lifecycle tests use explicit HTTP routes.
 * Script-local run names are rebound to the request's unique accepted ID. */
export function fakeChatBackend(scripts: Record<string, Handler>) {
  const routes: Record<string, Handler> = {};
  const histories = new Map<string, { session: { roleId: string; sessionKey: string }; messages: Message[] }>();
  const records = new Map<string, { run: ChatRunSnapshot; fixtureId: string; response: Reply; rolePath: string }>();
  for (const [path, handler] of Object.entries(scripts)) {
    if (!path.startsWith('CHAT ')) {
      routes[path] = async (init, match) => {
        const reply = await handler(init, match);
        if (path.endsWith('/session') && reply.body) histories.set(path.slice(4, -8), reply.body as ReturnType<typeof histories.get> & object);
        return reply;
      };
      continue;
    }
    const rolePath = path.slice(5);
    const roleId = rolePath.split('/').at(-1)!;
    routes[`POST ${rolePath}/runs`] = async (init, match) => {
      const input = JSON.parse(String(init?.body));
      const runId = `run-${input.requestId}`;
      const reply = await handler(init, match);
      if ((reply.status ?? 200) >= 400) return reply;
      const run: ChatRunSnapshot = { runId, roleId, sessionKey: `role:${roleId}`, sequence: 0,
        status: 'running', phase: 'model', stopping: false, tools: [] };
      records.set(runId, { run, fixtureId: reply.headers?.['X-Meido-Run-Id'] ?? 'run-1', response: reply, rolePath });
      return { body: { ...histories.get(rolePath), run } };
    };
    routes[`GET ${rolePath}/runs/([^/]+)`] = (_init, match) => {
      const record = records.get(match[1]);
      return record ? { body: { ...histories.get(rolePath), run: record.run } } : { status: 404 };
    };
    routes[`POST ${rolePath}/runs/([^/]+)/stop`] = async (init, match) => {
      const record = records.get(match[1]);
      const handler = record && scripts[`POST ${rolePath}/runs/${record.fixtureId}/stop`];
      if (!record || !handler) return { status: 404 };
      const reply = await handler(init, match);
      if ((reply.status ?? 200) < 400) record.run.status = 'cancelled';
      return reply;
    };
    routes[`GET ${rolePath}/runs/([^/]+)/events\\?after=([0-9-]+)`] = (_init, match) => {
      const record = records.get(match[1]);
      if (!record?.response.stream) return { status: 404 };
      const script = record.response.stream;
      const encoder = new TextEncoder(), decoder = new TextDecoder();
      const source = Array.isArray(script) ? new ReadableStream<Uint8Array>({ start(controller) {
        script.forEach(chunk => controller.enqueue(encoder.encode(chunk))); controller.close();
      } }) : script;
      const reader = source.getReader();
      let buffer = '';
      return { stream: new ReadableStream<Uint8Array>({ async pull(controller) {
        try {
          const { done, value } = await reader.read();
          buffer += decoder.decode(value, { stream: !done });
          const blocks = buffer.split('\n\n'); buffer = blocks.pop() ?? '';
          for (const block of blocks) {
            const lines = block.split('\n');
            const kind = lines.find(line => line.startsWith('event: '))?.slice(7);
            const raw = lines.find(line => line.startsWith('data: '))?.slice(6);
            if (!kind || !raw) continue;
            const data = JSON.parse(raw);
            if (!data.runId || data.runId === record.fixtureId) data.runId = record.run.runId;
            data.sequence ??= record.run.sequence + 1;
            if (data.message) data.message = { ...data.message, runId: data.message.runId ?? data.runId };
            const wire = sse(kind, data), event = parseStreamEvent(wire);
            if (event && event.runId === record.run.runId && data.sequence > record.run.sequence) {
              record.run.sequence = data.sequence;
              const history = histories.get(rolePath);
              if (history) history.messages = applyStreamEvent(history.messages, event, history.session.sessionKey);
              if (kind === 'assistant_completed' || kind === 'assistant_failed') record.run.status = kind === 'assistant_completed' ? 'completed' : 'failed';
            }
            controller.enqueue(encoder.encode(wire));
          }
          if (done) controller.close();
        } catch (error) { controller.error(error); }
      }, cancel() { void reader.cancel().catch(() => {}); } }) };
    };
  }
  return fakeBackend(routes);
}
