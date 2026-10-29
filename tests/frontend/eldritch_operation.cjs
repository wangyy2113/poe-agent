// Exercise the real Eldritch workbench with isolated DOM and API stubs.
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
  'eldritch-operation-open', 'eldritch-operation-dialog', 'eldritch-operation-close',
  'eldritch-operation-form', 'eldritch-operation-name',
  'eldritch-operation-catalog-status', 'eldritch-operation-mode',
  'eldritch-operation-action-note', 'eldritch-operation-manual',
  'eldritch-operation-item-wrap', 'eldritch-operation-item',
  'eldritch-operation-slot-wrap', 'eldritch-operation-dominant-wrap',
  'eldritch-operation-implicit-wrap', 'eldritch-operation-slot',
  'eldritch-operation-rarity', 'eldritch-operation-dominance',
  'eldritch-operation-prefix', 'eldritch-operation-suffix',
  'eldritch-operation-searing', 'eldritch-operation-eater',
  'eldritch-operation-corrupted', 'eldritch-operation-mirrored',
  'eldritch-operation-fractured', 'eldritch-operation-run',
  'eldritch-operation-status', 'eldritch-operation-result',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['eldritch-operation-mode'].value = 'manual';
const el = id => elements[`eldritch-operation-${id}`];
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
  path.resolve(__dirname, '../../static/eldritch-operation.js'), 'utf8'
), context);

const response = (data, status = 200) => ({
  ok: status === 200, status, json: async () => data,
});
const tick = () => new Promise(resolve => setImmediate(resolve));
const nodes = root => [root, ...root.children.flatMap(nodes)];
const catalog = { total: 4, items: [
  { name_zh: '次级古灵余烬', name_en: 'Lesser Eldritch Ember',
    action: 'set_implicit', source_contract_ok: true },
  { name_zh: '高级古灵余烬', name_en: 'Greater Eldritch Ember',
    action: 'set_implicit', source_contract_ok: false },
  { name_zh: '古灵崇高石', name_en: 'Eldritch Exalted Orb',
    action: 'add_dominant_side', source_contract_ok: true },
  { name_zh: '冲突石', name_en: 'Orb of Conflict',
    action: 'conflict', source_contract_ok: true },
] };
function result(overrides = {}) {
  return {
    found: true, verdict: 'compatible', operation: '次级古灵余烬',
    effect_text: '<script>alert(1)</script>', source: 'POEDB',
    source_url: 'https://poedb.tw/cn/Eldritch_Ember',
    fetched_at: '2026-10-01', metadata_id: 'ember-1',
    before: { rarity: null, item_slot: 'helmet', dominance: null,
      prefix_count: null, suffix_count: null, has_searing_implicit: true,
      has_eater_implicit: null, corrupted: false, mirrored: false, fractured: false },
    after: { prefix_count: null, suffix_count: null, has_searing_implicit: true,
      has_eater_implicit: null, searing_implicit_tier: '次级', eater_implicit_tier: null,
      explicit_modifier_change: 'unchanged',
      implicit_modifier_change: 'set_searing_and_replace_all_except_eater',
      random_outcome: true },
    checks: [{ key: 'source_contract', status: 'pass', summary: '原文逐字一致',
      source_url: 'https://poedb.tw/cn/Eldritch_Ember' }],
    unknown_fields: ['具体基底词缀'], metamod_interactions: [],
    boundary: '不生成随机词缀、概率或路线', ...overrides,
  };
}

async function main() {
  context.window.openEldritchOperation('物品类别: 头盔\n稀有度: 稀有');
  assert.equal(el('dialog').open, true);
  assert.equal(el('mode').value, 'item');
  assert.equal(requests[0].url, '/craft/eldritch/operations');
  assert.equal(el('run').disabled, true, 'loading the catalog must not submit an operation');
  requests[0].resolve(response(catalog));
  await tick();
  assert.equal(el('name').children.length, 5);
  assert.match(el('catalog-status').textContent, /4 种/);
  assert.equal(el('run').disabled, true);

  el('name').value = '次级古灵余烬';
  el('name').listeners.change();
  assert.equal(el('slot-wrap').hidden, false);
  assert.equal(el('implicit-wrap').hidden, false);
  assert.equal(el('dominant-wrap').hidden, true);
  el('mode').value = 'manual';
  el('mode').listeners.change();
  el('slot').value = 'helmet';
  el('searing').value = 'yes';
  let pending = context.eldritchSubmit({ preventDefault() {} });
  assert.equal(requests[1].url, '/craft/eldritch');
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    operation: '次级古灵余烬', corrupted: false, mirrored: false, fractured: false,
    item_slot: 'helmet', has_searing_implicit: true, has_eater_implicit: null,
  });
  requests[1].resolve(response(result()));
  await pending;
  assert.match(el('result').textContent, /已知约束兼容/);
  assert.match(el('result').textContent, /新基底档级：焚界者 次级/);
  assert.match(el('result').textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'SCRIPT').length, 0);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 2);

  el('name').value = '古灵崇高石';
  el('name').listeners.change();
  assert.equal(el('dominant-wrap').hidden, false);
  assert.equal(el('implicit-wrap').hidden, true);
  el('rarity').value = '稀有';
  el('dominance').value = '灭界者';
  el('prefix').value = '7';
  await context.eldritchSubmit({ preventDefault() {} });
  assert.match(el('status').textContent, /0 到 6/);
  assert.equal(requests.length, 2);
  el('prefix').value = '0';
  el('suffix').value = '';
  pending = context.eldritchSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[2].options.body), {
    operation: '古灵崇高石', corrupted: false, mirrored: false, fractured: false,
    rarity: '稀有', dominance: '灭界者', prefix_count: 0, suffix_count: null,
  });
  requests[2].resolve(response(result({
    verdict: 'unknown', operation: '古灵崇高石',
    after: { ...result().after, explicit_modifier_change: 'one_random_suffix_added',
      prefix_count: 0, suffix_count: null, searing_implicit_tier: null },
    metamod_interactions: [{ guard: '无法骰出攻击词缀', raw_value: 'YES',
      note: '<b>原注释</b>', source_url: 'https://www.craftofexile.com/metamods' }],
  })));
  await pending;
  assert.match(el('result').textContent, /信息不足/);
  assert.match(el('result').textContent, /前缀 0；后缀 未知/);
  assert.match(el('result').textContent, /YES；原始注释：<b>原注释<\/b>/);
  assert.match(el('result').textContent, /不解释外延/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 3);

  el('name').value = '冲突石';
  el('name').listeners.change();
  el('searing').value = 'yes';
  el('eater').value = 'no';
  pending = context.eldritchSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[3].options.body), {
    operation: '冲突石', corrupted: false, mirrored: false, fractured: false,
    has_searing_implicit: true, has_eater_implicit: false,
  });
  requests[3].resolve(response(result({
    verdict: 'incompatible', checks: [
      { key: 'source_contract', status: 'pass', summary: '原文逐字一致' },
      { key: 'eater_implicit', status: 'fail', summary: '缺少灭界者基底' },
    ],
  })));
  await pending;
  assert.match(el('result').textContent, /已知约束不兼容/);
  assert.match(el('result').textContent, /不是可执行结果/);

  el('name').value = '高级古灵余烬';
  el('name').listeners.change();
  assert.match(el('action-note').textContent, /来源效果原文已变化/);
  pending = context.eldritchSubmit({ preventDefault() {} });
  requests[4].resolve(response(result({
    verdict: 'unknown', source_url: 'https://poedb.tw.evil.example/Ember',
    checks: [{ key: 'source_contract', status: 'unknown', summary: '原文已变化',
      source_url: 'javascript:alert(1)' }],
    after: { ...result().after, implicit_modifier_change: 'set_searing_and_replace_all_except_eater' },
  })));
  await pending;
  assert.match(el('result').textContent, /状态转移未知，不沿用旧操作语义/);
  assert.doesNotMatch(el('result').textContent, /添加指定档级焚界者基底词缀/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 0);

  el('name').value = '古灵崇高石';
  el('name').listeners.change();
  el('mode').value = 'item';
  el('mode').listeners.change();
  assert.match(el('action-note').textContent, /不能确定首要方/);
  pending = context.eldritchSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[5].options.body), {
    operation: '古灵崇高石', item_text: '物品类别: 头盔\n稀有度: 稀有',
  });
  requests[5].resolve(response({ detail: { reason: '装备文本解析失败' } }, 422));
  await pending;
  assert.match(el('status').textContent, /装备文本解析失败/);

  pending = context.eldritchSubmit({ preventDefault() {} });
  el('item').value = 'new item';
  el('form').listeners.input();
  requests[6].resolve(response(result({ effect_text: '过期响应 1' })));
  await pending;
  assert.doesNotMatch(el('result').textContent, /过期响应/);

  pending = context.eldritchSubmit({ preventDefault() {} });
  el('dialog').close();
  requests[7].resolve(response(result({ effect_text: '过期响应 2' })));
  await pending;
  assert.doesNotMatch(el('result').textContent, /过期响应/);

  context.window.openEldritchOperation('fresh item');
  assert.equal(requests[8].url, '/craft/eldritch/operations');
  context.window.clearEldritchOperation();
  requests[8].resolve(response(catalog));
  await tick();
  assert.equal(el('dialog').open, false);
  assert.equal(el('name').children.length, 0);
  assert.equal(el('item').value, '');
  assert.equal(el('result').textContent, '');

  context.window.openEldritchOperation('item after key change');
  requests[9].resolve(response(catalog));
  await tick();
  el('name').value = '次级古灵余烬';
  pending = context.eldritchSubmit({ preventDefault() {} });
  assert.equal(requests[10].url, '/craft/eldritch');
  context.window.clearEldritchOperation();
  requests[10].resolve(response(result({ effect_text: '过期响应 3' })));
  await pending;
  assert.equal(el('dialog').open, false);
  assert.doesNotMatch(el('result').textContent, /过期响应/);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
