// Exercise the real essence workbench with DOM and API stubs.
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
  'essence-operation-open', 'essence-operation-dialog', 'essence-operation-close',
  'essence-operation-form', 'essence-operation-query', 'essence-operation-name',
  'essence-operation-catalog-status', 'essence-operation-mode',
  'essence-operation-manual', 'essence-operation-item-wrap',
  'essence-operation-item', 'essence-operation-rarity', 'essence-operation-level',
  'essence-operation-class', 'essence-operation-corrupted',
  'essence-operation-mirrored', 'essence-operation-fractured',
  'essence-operation-run', 'essence-operation-status', 'essence-operation-result',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['essence-operation-mode'].value = 'manual';
const el = id => elements[`essence-operation-${id}`];
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
  path.resolve(__dirname, '../../static/essence-operation.js'), 'utf8'
), context);

const response = (data, status = 200) => ({
  ok: status === 200, status, json: async () => data,
});
const tick = () => new Promise(resolve => setImmediate(resolve));
const nodes = root => [root, ...root.children.flatMap(nodes)];
const catalog = { total: 3, items: [
  { name_zh: '哀恸精华', name_en: 'Essence of Desolation', tier: 0, level_cap: 15 },
  { name_zh: '憎恨之低语精华', name_en: 'Whispering Essence of Hatred', tier: 1, level_cap: 35 },
  { name_zh: '憎恨之破空精华', name_en: 'Deafening Essence of Hatred', tier: 7, level_cap: null },
] };
function result(overrides = {}) {
  const guaranteed = { item_class: '项链', text: '<img src=x onerror=alert(1)>' };
  return {
    found: true, verdict: 'compatible', essence: '憎恨之破空精华',
    tier: 7, level_cap: null, base_effect: '<script>alert(1)</script>',
    source: 'POEDB', fetched_at: '2026-10-01', metadata_id: 'essence-7',
    source_url: 'https://poedb.tw/cn/Essence',
    before: { rarity: '稀有', item_level: 84, item_class: '项链',
      corrupted: false, mirrored: false, fractured: false },
    after: { rarity: '稀有', prefix_count: null, suffix_count: null,
      operation_mode: 'reroll_rare_properties', guaranteed_modifier: guaranteed },
    guaranteed_modifier: guaranteed,
    checks: [{ key: 'rarity', status: 'pass', summary: '来源原文允许',
      source_url: 'https://poedb.tw/cn/Essence' }],
    unknown_fields: ['额外词缀数量'], boundary: '不生成成功率或路线',
    ...overrides,
  };
}

async function main() {
  // The paste card only opens and pre-fills; its first call loads the exact-name catalog.
  context.window.openEssenceOperation('物品类别: 项链\n稀有度: 稀有\n--------\n物品等级: 84');
  assert.equal(el('dialog').open, true);
  assert.equal(el('mode').value, 'item');
  assert.equal(el('manual').hidden, true);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/craft/essences');
  assert.equal(el('run').disabled, true);
  requests[0].resolve(response(catalog));
  await tick();
  assert.match(el('catalog-status').textContent, /当前入库 3 件/);
  assert.equal(el('name').children.length, 4);
  assert.equal(el('run').disabled, true, 'catalog load must not select an essence');

  el('query').value = 'T7';
  el('query').listeners.input();
  assert.equal(el('name').children.length, 2);
  assert.match(el('name').children[1].textContent, /憎恨之破空精华/);
  el('query').value = 'hatred';
  el('query').listeners.input();
  assert.equal(el('name').children.length, 3);
  el('name').value = '憎恨之破空精华';
  el('form').listeners.change();
  assert.equal(el('run').disabled, false);

  // Manual unknown fields remain null; a source level limit is never inferred from tier.
  el('mode').value = 'manual';
  el('mode').listeners.change();
  el('rarity').value = '普通';
  el('level').value = '';
  el('class').value = '';
  let pending = context.essenceSubmit({ preventDefault() {} });
  assert.equal(requests[1].url, '/craft/essence');
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    essence: '憎恨之破空精华', rarity: '普通', item_level: null,
    item_class: null, corrupted: false, mirrored: false, fractured: false,
  });
  assert.equal(el('run').disabled, true);
  requests[1].resolve(response(result()));
  await pending;
  assert.match(el('result').textContent, /已知约束兼容/);
  assert.match(el('result').textContent, /前后缀数量：未知/);
  assert.match(el('result').textContent, /<script>alert\(1\)<\/script>/);
  assert.match(el('result').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'SCRIPT').length, 0);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 2);

  el('level').value = '101';
  el('form').listeners.input();
  assert.equal(el('result').hidden, true);
  await context.essenceSubmit({ preventDefault() {} });
  assert.match(el('status').textContent, /1 到 100/);
  assert.equal(requests.length, 2);
  el('level').value = '35';
  el('class').value = '项链';

  pending = context.essenceSubmit({ preventDefault() {} });
  requests[2].resolve(response(result({
    verdict: 'incompatible', level_cap: 35,
    source_url: 'https://poedb.tw.evil.example/Essence',
    checks: [{ key: 'item_level', status: 'fail', summary: '<b>超过上限</b>',
      source_url: 'javascript:alert(1)' }],
  })));
  await pending;
  assert.match(el('result').textContent, /已知约束不兼容/);
  assert.match(el('result').textContent, /不是可执行结果/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 0);

  // Source effect drift cannot keep the old guaranteed modifier as an outcome.
  pending = context.essenceSubmit({ preventDefault() {} });
  requests[3].resolve(response(result({
    verdict: 'unknown', after: { rarity: '稀有', operation_mode: 'unknown',
      guaranteed_modifier: null },
    checks: [{ key: 'source_contract', status: 'unknown', summary: '原文已变化' }],
  })));
  await pending;
  assert.match(el('result').textContent, /信息不足/);
  assert.match(el('result').textContent, /不执行状态转移/);
  assert.match(el('result').textContent, /保底词缀：未知/);
  assert.doesNotMatch(el('result').textContent, /<img src=x/);

  // Item text never mixes with the old manual state fields.
  el('mode').value = 'item';
  el('mode').listeners.change();
  pending = context.essenceSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[4].options.body), {
    essence: '憎恨之破空精华',
    item_text: '物品类别: 项链\n稀有度: 稀有\n--------\n物品等级: 84',
  });
  requests[4].resolve(response({ detail: { reason: '装备文本解析失败' } }, 422));
  await pending;
  assert.match(el('status').textContent, /装备文本解析失败/);

  pending = context.essenceSubmit({ preventDefault() {} });
  el('item').value = 'new item';
  el('form').listeners.input();
  requests[5].resolve(response(result({ base_effect: '过期响应 1' })));
  await pending;
  assert.doesNotMatch(el('result').textContent, /过期响应/);

  pending = context.essenceSubmit({ preventDefault() {} });
  el('dialog').close();
  requests[6].resolve(response(result({ base_effect: '过期响应 2' })));
  await pending;
  assert.doesNotMatch(el('result').textContent, /过期响应/);

  // Closing invalidates an in-flight catalog; key cleanup clears the whole form.
  context.window.openEssenceOperation('fresh item');
  assert.equal(requests[7].url, '/craft/essences');
  context.window.clearEssenceOperation();
  requests[7].resolve(response(catalog));
  await tick();
  assert.equal(el('dialog').open, false);
  assert.equal(el('name').children.length, 0);
  assert.equal(el('item').value, '');
  assert.equal(el('mode').value, 'manual');
  assert.equal(el('result').textContent, '');

  context.window.openEssenceOperation('item after key change');
  requests[8].resolve(response(catalog));
  await tick();
  el('name').value = '哀恸精华';
  pending = context.essenceSubmit({ preventDefault() {} });
  assert.equal(requests[9].url, '/craft/essence');
  context.window.clearEssenceOperation();
  requests[9].resolve(response(result({ base_effect: '过期响应 3' })));
  await pending;
  assert.equal(el('dialog').open, false);
  assert.doesNotMatch(el('result').textContent, /过期响应/);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
