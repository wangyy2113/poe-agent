// Exercise the conflict browser with the actual script and an isolated DOM/API stub.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this._text = '';
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.open = false;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('untrusted HTML injection'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() {}
  showModal() { this.open = true; }
  close() { this.open = false; this.listeners.close?.(); }
  reset() {}
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

const ids = [
  'conflict-open', 'conflict-dialog', 'conflict-close', 'conflict-form',
  'conflict-query', 'conflict-kind-status', 'conflict-resolution',
  'conflict-kind', 'conflict-source', 'conflict-search',
  'conflict-summary-status', 'conflict-summary', 'conflict-status',
  'conflict-results', 'conflict-prev', 'conflict-next', 'conflict-page',
  'conflict-detail',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['conflict-kind-status'].value = 'confirmed';
elements['conflict-resolution'].value = 'active';
const requests = [];
const context = vm.createContext({
  document: { getElementById: id => elements[id], createElement: tag => new Element(tag) },
  window: {}, URL,
  apiFetch: (url, options) => {
    const next = deferred();
    requests.push({ url, options, next });
    return next.promise;
  },
});
vm.runInContext(fs.readFileSync(
  path.resolve(__dirname, '../../static/conflict-ledger.js'), 'utf8'
), context);

function response(data, status = 200) {
  return { ok: status === 200, status, json: async () => data };
}
function tick() { return new Promise(resolve => setImmediate(resolve)); }
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function row(overrides = {}) {
  return {
    conflict_id: 42, subject: 'spectre=Cursed Mariner', field: 'life_damage',
    kind: 'spectre', status: 'confirmed', source_a: 'POEDB', value_a: '生命 100%',
    source_b: 'Path of Building', value_b: '生命 1.0×', cause: null,
    resolved: false, first_seen: '2026-01-01T00:00:00Z',
    last_seen: '2026-10-28T00:00:00Z', ...overrides,
  };
}
function page(items, more = false, cursor = null) {
  return { items, returned: items.length, total_matching: more ? 2 : items.length,
    confirmed_count: items.filter(item => item.status === 'confirmed').length,
    review_count: items.filter(item => item.status === 'review').length,
    has_more: more, next_cursor: cursor };
}

async function main() {
  elements['conflict-open'].listeners.click();
  assert.equal(elements['conflict-dialog'].open, true);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].url, '/conflicts/summary');
  assert.equal(requests[1].url, '/conflicts/lookup');
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    query: '', status: 'confirmed', resolution: 'active',
    kinds: [], sources: [], limit: 20, cursor: null,
  });
  requests[0].next.resolve(response({
    confirmed_active_count: 3, review_active_count: 4,
    confirmed_count: 5, review_count: 6,
    review_causes: [{ cause: 'ladder_partial', review_count: 2 }],
  }));
  requests[1].next.resolve(response(page([row()])));
  await tick();
  assert.match(elements['conflict-summary'].textContent, /待核对成因：ladder_partial 2 条/);
  assert.match(elements['conflict-results'].textContent, /已确认冲突/);
  assert.doesNotMatch(elements['conflict-results'].textContent, /待核对差异/);

  const detailButton = nodes(elements['conflict-results'])
    .find(node => node.tagName === 'BUTTON');
  detailButton.listeners.click();
  assert.equal(requests[2].url, '/conflicts/42');
  requests[2].next.resolve(response(row({
    ref_a: 'poedb:42', ref_b: 'pob:42',
    url_a: 'https://poedb.tw/cn/example', url_b: 'https://github.com/example',
    value_a_length: 7, value_b_length: 7,
    value_a_truncated: false, value_b_truncated: false,
    can_support_facts: true, boundary: '只并列展示，不裁决',
  })));
  await tick();
  assert.match(elements['conflict-detail'].textContent, /poedb:42/);
  const safeLinks = nodes(elements['conflict-detail']).filter(node => node.tagName === 'A');
  assert.equal(safeLinks.length, 2);
  assert.equal(safeLinks[0].rel, 'noopener noreferrer');

  elements['conflict-kind-status'].value = 'review';
  elements['conflict-form'].listeners.change();
  assert.equal(elements['conflict-detail'].hidden, true);
  elements['conflict-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[3].url, '/conflicts/lookup');
  assert.equal(JSON.parse(requests[3].options.body).status, 'review');
  const review = row({
    conflict_id: 43, kind: 'mod_review', status: 'review', cause: 'ladder_partial',
    value_a: '<script>alert(1)</script>', value_b: 'B',
  });
  requests[3].next.resolve(response(page([review], true, 'cursor-2')));
  await tick();
  assert.match(elements['conflict-results'].textContent, /待核对差异/);
  assert.match(elements['conflict-status'].textContent, /不能视为已确认冲突/);
  assert.match(elements['conflict-results'].textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(elements['conflict-next'].disabled, false);

  const reviewButton = nodes(elements['conflict-results'])
    .find(node => node.tagName === 'BUTTON');
  reviewButton.listeners.click();
  requests[4].next.resolve(response({ ...review, value_a_length: 20000,
    value_b_length: 1, value_a_truncated: true, value_b_truncated: false,
    ref_a: null, ref_b: null, url_a: 'javascript:alert(1)',
    url_b: 'https://example.com/not-allowlisted',
    can_support_facts: false, boundary: 'review 不能支撑事实' }));
  await tick();
  assert.match(elements['conflict-detail'].textContent, /前 16000 字/);
  assert.match(elements['conflict-detail'].textContent, /review 不能支撑事实/);
  assert.equal(nodes(elements['conflict-detail']).filter(node => node.tagName === 'A').length, 0);

  elements['conflict-next'].listeners.click();
  assert.equal(JSON.parse(requests[5].options.body).cursor, 'cursor-2');
  requests[5].next.resolve(response(page([row({ conflict_id: 41,
    kind: 'tree_review', status: 'review' })])));
  await tick();
  assert.match(elements['conflict-page'].textContent, /第 2 页/);
  elements['conflict-prev'].listeners.click();
  assert.equal(JSON.parse(requests[6].options.body).cursor, null);
  requests[6].next.resolve(response(page([review], true, 'cursor-2')));
  await tick();

  const old = context.conflictLoadDetail(43);
  elements['conflict-query'].value = '新的条件';
  elements['conflict-form'].listeners.input();
  elements['conflict-form'].listeners.submit({ preventDefault() {} });
  assert.equal(JSON.parse(requests[8].options.body).query, '新的条件');
  requests[7].next.resolve(response({ ...review, boundary: '过期详情' }));
  await old;
  assert.doesNotMatch(elements['conflict-detail'].textContent, /过期详情/);
  requests[8].next.resolve(response(page([])));
  await tick();
  assert.match(elements['conflict-results'].textContent, /没有匹配记录/);

  context.window.clearConflictLedger();
  assert.equal(elements['conflict-dialog'].open, false);
  assert.equal(elements['conflict-query'].value, '');
  assert.equal(elements['conflict-results'].textContent, '');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
