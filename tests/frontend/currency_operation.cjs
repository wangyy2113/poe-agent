// Exercise the real currency operation browser script with isolated DOM/API stubs.
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
    this.checked = false;
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

const ids = [
  'currency-operation-open', 'currency-operation-dialog', 'currency-operation-close',
  'currency-operation-form', 'currency-operation-name', 'currency-operation-mode',
  'currency-operation-manual', 'currency-operation-item-wrap', 'currency-operation-item',
  'currency-operation-rarity', 'currency-operation-kind', 'currency-operation-prefix',
  'currency-operation-suffix', 'currency-operation-corrupted',
  'currency-operation-mirrored', 'currency-operation-fractured',
  'currency-operation-guard', 'currency-operation-run', 'currency-operation-status',
  'currency-operation-result',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['currency-operation-mode'].value = 'manual';
elements['currency-operation-kind'].value = 'default';
const requests = [];
const context = vm.createContext({
  document: { getElementById: id => elements[id], createElement: tag => new Element(tag) },
  window: {}, URL,
  apiFetch: (url, options) => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    requests.push({ url, options, resolve });
    return promise;
  },
});
vm.runInContext(fs.readFileSync(
  path.resolve(__dirname, '../../static/currency-operation.js'), 'utf8'
), context);

const el = id => elements[`currency-operation-${id}`];
const nodes = root => [root, ...root.children.flatMap(nodes)];
const response = (data, status = 200) => ({
  ok: status === 200, status, json: async () => data,
});
function result(overrides = {}) {
  return {
    found: true, verdict: 'compatible', operation_verdict: 'compatible',
    operation: '崇高石', effect_text: '<script>alert(1)</script>',
    source: 'POEDB', source_url: 'https://poedb.tw/cn/Exalted_Orb',
    fetched_at: '2026-10-01', metadata_id: 'currency-1',
    before: { rarity: '稀有', prefix_count: 2, suffix_count: 3,
      item_kind: 'default', corrupted: false, mirrored: false, fractured: false },
    after: { rarity: '稀有', prefix_count: 3, suffix_count: 3,
      modifier_identities: 'one_random_modifier_added', modifier_values: 'unchanged' },
    checks: [{ key: '稀有度', status: 'pass', summary: '条件满足',
      source_url: 'https://poedb.tw/cn/Exalted_Orb' }],
    unknown_fields: ['具体结果词缀'], metamod_guard: '',
    metamod_interaction: { status: 'not_requested' },
    boundary: '不生成结果词缀、成功率、路线', ...overrides,
  };
}

async function main() {
  el('open').listeners.click();
  assert.equal(el('dialog').open, true);
  assert.equal(requests.length, 0, 'opening must not submit');

  // Empty fields remain unknown; an explicit zero remains zero.
  el('name').value = '崇高石';
  el('prefix').value = '0';
  let pending = context.currencySubmit({ preventDefault() {} });
  assert.equal(requests[0].url, '/craft/operation');
  assert.equal(requests[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    operation: '崇高石', metamod_guard: null, rarity: null,
    prefix_count: 0, suffix_count: null, item_kind: 'default',
    corrupted: false, mirrored: false, fractured: false,
  });
  assert.equal(el('run').disabled, true);
  requests[0].resolve(response(result()));
  await pending;
  assert.equal(el('run').disabled, false);
  assert.equal(el('result').hidden, false);
  assert.match(el('result').textContent, /已知约束兼容/);
  assert.match(el('result').textContent, /具体结果词缀/);
  assert.match(el('result').textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'SCRIPT').length, 0);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 2);

  // Input changes remove old conclusions before another request is made.
  el('suffix').value = '2';
  el('form').listeners.input();
  assert.equal(el('result').hidden, true);
  assert.equal(el('result').textContent, '');
  el('rarity').value = '稀有';
  el('prefix').value = '7';
  await context.currencySubmit({ preventDefault() {} });
  assert.match(el('status').textContent, /0 到 6/);
  assert.equal(requests.length, 1);
  el('prefix').value = '2';

  // Equipment text is the sole state input in item mode, including after paste prefilling.
  context.window.openCurrencyOperation('Rarity: Rare\n--------\nTest Item');
  assert.equal(el('mode').value, 'item');
  assert.equal(el('manual').hidden, true);
  assert.equal(el('item-wrap').hidden, false);
  assert.equal(requests.length, 1, 'paste prefilling must not submit');
  el('guard').value = '前缀无法被变更';
  pending = context.currencySubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    operation: '崇高石', metamod_guard: '前缀无法被变更',
    item_text: 'Rarity: Rare\n--------\nTest Item',
  });
  requests[1].resolve(response(result({
    verdict: 'unknown', operation_verdict: 'compatible',
    before: { ...result().before, incomplete_reason: '一条显式词缀无法分类' },
    metamod_guard: '前缀无法被变更',
    metamod_interaction: { status: 'source_blank', operation: '崇高石',
      guard: '前缀无法被变更', reason: '来源空白' },
  })));
  await pending;
  assert.match(el('result').textContent, /基础通货单独结论：已知约束兼容/);
  assert.match(el('result').textContent, /整体结论：信息不足/);
  assert.match(el('result').textContent, /来源空白/);
  assert.match(el('result').textContent, /未完整识别/);

  // Recorded matrix values stay literal, separate from the currency state.
  pending = context.currencySubmit({ preventDefault() {} });
  requests[2].resolve(response(result({
    verdict: 'incompatible', operation_verdict: 'incompatible',
    source_url: 'https://poedb.tw.evil.example/currency',
    checks: [{ key: '名额', status: 'fail', summary: '<b>已满</b>',
      source_url: 'javascript:alert(1)' }],
    after: { ...result().after, possible_rarities: ['稀有', '传奇'],
      possible_affix_counts: [{ prefix_count: 2, suffix_count: 3 }] },
    metamod_guard: '前缀无法被变更',
    metamod_interaction: { status: 'recorded', operation: '崇高石',
      guard: '前缀无法被变更', raw_value: 'YES & NO', note_text: '<b>原始注释</b>',
      version_scope: 'unknown', source_url: 'https://www.craftofexile.com/metamods' },
  })));
  await pending;
  assert.match(el('result').textContent, /已知约束不兼容/);
  assert.match(el('result').textContent, /YES & NO/);
  assert.match(el('result').textContent, /可能稀有度：稀有 \/ 传奇/);
  assert.match(el('result').textContent, /<b>原始注释<\/b>/);
  const links = nodes(el('result')).filter(node => node.tagName === 'A');
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://www.craftofexile.com/metamods');
  assert.equal(links[0].rel, 'noopener noreferrer');

  pending = context.currencySubmit({ preventDefault() {} });
  requests[3].resolve(response({ detail: { reason: '装备文本解析失败' } }, 422));
  await pending;
  assert.match(el('status').textContent, /装备文本解析失败/);
  assert.equal(el('result').hidden, true);

  // New input, close, and API-key cleanup each invalidate in-flight responses.
  pending = context.currencySubmit({ preventDefault() {} });
  el('item').value = 'new item';
  el('form').listeners.input();
  requests[4].resolve(response(result({ effect_text: '过期响应 1' })));
  await pending;
  assert.doesNotMatch(el('result').textContent, /过期响应/);

  pending = context.currencySubmit({ preventDefault() {} });
  el('dialog').close();
  requests[5].resolve(response(result({ effect_text: '过期响应 2' })));
  await pending;
  assert.doesNotMatch(el('result').textContent, /过期响应/);

  context.window.openCurrencyOperation('fresh item');
  pending = context.currencySubmit({ preventDefault() {} });
  context.window.clearCurrencyOperation();
  requests[6].resolve(response(result({ effect_text: '过期响应 3' })));
  await pending;
  assert.equal(el('dialog').open, false);
  assert.equal(el('item').value, '');
  assert.equal(el('mode').value, 'manual');
  assert.equal(el('name').value, '');
  assert.equal(el('result').textContent, '');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
