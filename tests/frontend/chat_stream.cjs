const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../static/app.js'), 'utf8');
// Exercise the real sender and event handler with a byte stream and UI spies.
const sender = source.slice(source.indexOf('async function send(text)'), source.indexOf('/* ── 粘贴识别'));
async function run(events, split = false) {
  const body = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
  const bytes = new TextEncoder().encode(body);
  const chunks = split ? Array.from(bytes, byte => Uint8Array.of(byte)) : [bytes];
  const calls = [];
  const reader = {
    async read() { return chunks.length ? { value: chunks.shift(), done: false } : { done: true }; },
    async cancel() { calls.push(['cancel']); },
    releaseLock() { calls.push(['release']); },
  };
  const msg = Object.fromEntries(['setStatus', 'appendSentence', 'appendBlocked', 'appendGap',
    'appendCitations', 'appendNote', 'appendError', 'finish'].map(name => [name, (...args) => calls.push([name, ...args])]));
  const context = vm.createContext({
    TextDecoder, busy: false, authRequired: false, conversationId: 'conv',
    el: { send: {}, input: { value: '', focus() {} } },
    addUserMessage() {}, autoGrow() {}, refreshConversations() {},
    addAssistantMessage: () => msg,
    apiFetch: async () => ({ ok: true, body: { getReader: () => reader } }),
  });
  vm.runInContext(sender, context);
  await context.send('question');
  assert.equal(context.busy, false);
  assert.equal(context.el.send.disabled, false);
  assert.equal(calls.filter(c => c[0] === 'cancel').length, 1);
  assert.equal(calls.filter(c => c[0] === 'release').length, 1);
  return calls;
}
(async () => {
  const sentence = { type: 'sentence', text: '中文正文。', kind: 'narrative' };
  const done = { type: 'done', trace_id: 'trace' };
  const success = await run([sentence, done], true);
  assert.equal(success.filter(c => c[0] === 'appendError').length, 0);
  assert.equal(success.find(c => c[0] === 'appendSentence')[1], sentence.text);
  assert.equal(success.find(c => c[0] === 'finish')[1].type, 'done');
  const broken = await run([sentence]);
  assert.match(broken.find(c => c[0] === 'appendError')[1], /未完成/);
  assert.equal(broken.find(c => c[0] === 'finish')[1], null);
  const failed = await run([sentence, { type: 'error', message: '中断' }, done]);
  assert.deepEqual(failed.filter(c => c[0] === 'appendError'), [['appendError', '中断']]);
  assert.equal(failed.find(c => c[0] === 'finish')[1], null);
  console.log('Chat streaming: completion, fragmented UTF-8, failure and disconnect passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
