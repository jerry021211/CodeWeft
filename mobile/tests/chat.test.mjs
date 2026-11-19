import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { baseURL, readReply, streamChat } from '../src/shared/provider.ts';
import { History } from '../src/shared/history.ts';

function chunks(parts) {
  return new ReadableStream({ start(controller) { for (const part of parts) controller.enqueue(part); controller.close(); } });
}
const encode = text => new TextEncoder().encode(text);
const frame = text => 'data: ' + JSON.stringify({ choices: [{ delta: { content: text } }] }) + '\r\n\r\n';
const profile = { base: 'https://example.org/v1/', key: 'fixture', model: 'test' };

test('mobile request directly targets the configured API and handles split Unicode/SSE frames', async () => {
  const msgs = [{ role: 'user', content: '你好' }];
  let text = '';
  await streamChat(async (url, init) => {
    assert.equal(url, 'https://example.org/v1/chat/completions');
    assert.equal(init.headers.Authorization, 'Bearer fixture');
    assert.deepEqual(JSON.parse(init.body), { messages: msgs, model: 'test', stream: true });
    const data = encode(': heartbeat\r\n\r\n' + frame('你好') + frame('🙂') + 'data: [DONE]\n\n');
    return new Response(chunks(Array.from(data, x => Uint8Array.of(x))), { headers: { 'Content-Type': 'text/event-stream' } });
  }, profile, msgs, new AbortController().signal, part => { text += part; });
  assert.equal(text, '你好🙂');
});
test('premature EOF or provider error retains partial output and is never retried', async () => {
  for (const suffix of ['', 'data: nope\n\n', 'data: {"error":{"message":"private raw response"}}\n\n']) {
    let text = '';
    await assert.rejects(readReply(chunks([encode(frame('部分') + suffix)]), x => { text += x; }), e => !e.message.includes('private raw'));
    assert.equal(text, '部分');
  }
});
test('user cancellation interrupts an open stream via the request signal', async () => {
  const stop = new AbortController();
  let text = '';
  await assert.rejects(streamChat(async (_url, { signal }) => new Response(new ReadableStream({
    start(ctrl) {
      ctrl.enqueue(encode(frame('partial')));
      signal.addEventListener('abort', () => ctrl.error(new DOMException('aborted', 'AbortError')));
    },
  }), { headers: { 'Content-Type': 'text/event-stream' } }), profile, [], stop.signal, x => { text += x; stop.abort(); }), { name: 'AbortError' });
  assert.equal(text, 'partial');
});
test('rejects insecure or credential-bearing addresses and reports sanitized HTTP errors', async () => {
  for (const url of ['http://example.org', 'https://a:b@example.org', 'https://example.org/?key=x'])
    assert.throws(() => baseURL(url));
  assert.equal(baseURL('https://example.org/v1/chat/completions/'), 'https://example.org/v1');
  await assert.rejects(streamChat(async () => new Response('private detail', { status: 401 }), profile, [], new AbortController().signal, () => {}), /HTTP 401/);
});

// Use the actual schema on disk. Adapt Node SQLite's sync operations to the
// native adapter so recovery is exercised across two distinct DB connections.
function connect(path) {
  const conn = new DatabaseSync(path);
  return { close: () => conn.close(), history: new History({
    execSync: sql => conn.exec(sql),
    runSync: (sql, ...args) => { const result = conn.prepare(sql).run(...args); return { lastInsertRowId: Number(result.lastInsertRowid) }; },
    getAllSync: (sql, ...args) => conn.prepare(sql).all(...args),
    getFirstSync: (sql, ...args) => conn.prepare(sql).get(...args),
    withTransactionSync: fn => { conn.exec('BEGIN'); try { fn(); conn.exec('COMMIT'); } catch (e) { conn.exec('ROLLBACK'); throw e; } },
  }) };
}
test('reopening the database restores the selected chat and partial reply without resubmission', () => {
  const dir = mkdtempSync(join(tmpdir(), 'codeweft-mobile-'));
  const path = join(dir, 'chat.db');
  let handle = connect(path);
  try {
    const {chat, reply} = handle.history.begin(null, profile.base, profile.model, 'first');
    handle.history.save(reply, 'part', 'streaming');
    handle.close(); handle = connect(path);
    const history = handle.history;
    assert.equal(history.current().id, chat.id);
    assert.deepEqual(history.messages(chat.id).map(r => [r.content, r.state]), [['first','done'], ['part','interrupted']]);
    assert.throws(() => history.begin(chat, 'https://other.org', profile.model, 'do not send'));
    const second = history.begin(null, profile.base, profile.model, 'second');
    assert.equal(history.current().id, second.chat.id);
    assert.deepEqual(history.sessions().map(x => x.title), ['second','first']);
    history.select(chat.id);
    assert.equal(history.messages(chat.id).length, 2);
  } finally { handle.close(); rmSync(dir, { recursive: true }); }
});
