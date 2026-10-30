// Exercise the real Veiled workbench with DOM and API stubs.
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
  'open', 'dialog', 'close', 'form', 'name', 'catalog-status', 'mode',
  'action-note', 'manual', 'item-wrap', 'item', 'rarity', 'class',
  'prefix', 'suffix', 'corrupted', 'mirrored', 'fractured', 'run',
  'status', 'result',
];
const elements = Object.fromEntries(ids.map(id =>
  [`veiled-operation-${id}`, new Element(id)]));
elements['veiled-operation-mode'].value = 'manual';
const el = id => elements[`veiled-operation-${id}`];
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
  path.resolve(__dirname, '../../static/veiled-operation.js'), 'utf8'
), context);

const response = (data, status = 200) => ({ ok: status === 200, status, json: async () => data });
const tick = () => new Promise(resolve => setImmediate(resolve));
const nodes = root => [root, ...root.children.flatMap(nodes)];
const catalog = { total: 2, items: [
  { name_zh: '加密崇高石', name_en: 'Veiled Exalted Orb',
    action: 'remove_one_add_veiled', source_contract_ok: true },
  { name_zh: '加密混沌石', name_en: 'Veiled Chaos Orb',
    action: 'reroll_all_with_veiled', source_contract_ok: false },
] };
const candidates = Array.from({ length: 23 }, (_, index) => ({
  mod_code: `Veiled${index}`, name_zh: `词缀${index}`,
  position: index % 2 ? '后缀' : '前缀', group: 'Group',
  item_level: 60, weight: 1000, text: `<script>词缀${index}</script>`,
  url: index === 0 ? 'https://poedb.tw.evil.example/cn/Modifiers' :
    'https://poedb.tw/cn/Modifiers',
}));
function result(overrides = {}) {
  return {
    found: true, verdict: 'compatible', operation: '加密崇高石',
    effect_text: '<script>效果</script>', source: 'POEDB',
    source_url: 'https://poedb.tw/cn/Veiled_Exalted_Orb',
    fetched_at: '2026-10-30', metadata_id: 'veiled-1',
    before: { rarity: '稀有', item_class: '胸甲', prefix_count: 2, suffix_count: 2,
      corrupted: false, mirrored: false, fractured: false },
    after: { rarity: '稀有', prefix_count: null, suffix_count: null,
      modifier_change: 'one_random_modifier_removed_and_one_random_veiled_modifier_added',
      has_veiled_modifier: true, veiled_modifier_position: ['前缀', '后缀'],
      possible_affix_counts: [{ prefix_count: 1, suffix_count: 3 },
        { prefix_count: 2, suffix_count: 2 }], random_outcome: true },
    checks: [{ key: 'source_contract', status: 'pass', summary: '逐字一致',
      source_url: 'https://poedb.tw/cn/Veiled_Exalted_Orb' }],
    unknown_fields: ['具体结果概率'],
    veiled_pool: { item_class: '胸甲', scope: '2 个 POEDB 底材页面',
      source_rows: 25, unique_candidates: 23, prefix_candidates: 12,
      suffix_candidates: 11, candidates,
      caveat: '目录不是单次揭露选项；weight 不是结果概率' },
    metamod_interactions: [{ guard: '前缀无法被变更', raw_value: 'YES',
      note: '<b>原注释</b>', source_url: 'https://www.craftofexile.com/metamods' }],
    boundary: '不提供完整执行结论', ...overrides,
  };
}

async function main() {
  context.window.openVeiledOperation('物品类别: 胸甲\n稀有度: 稀有');
  assert.equal(el('dialog').open, true);
  assert.equal(el('mode').value, 'item');
  assert.equal(requests[0].url, '/craft/veiled/operations');
  assert.equal(el('run').disabled, true);
  requests[0].resolve(response(catalog));
  await tick();
  assert.equal(el('name').children.length, 3);
  assert.equal(el('run').disabled, true, 'loading must not select an operation');
  assert.match(el('name').textContent, /来源效果已变化/);

  el('name').value = '加密崇高石';
  el('name').listeners.change();
  assert.equal(el('item-wrap').hidden, false);
  let pending = context.veiledSubmit({ preventDefault() {} });
  assert.equal(requests[1].url, '/craft/veiled');
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    operation: '加密崇高石', item_text: '物品类别: 胸甲\n稀有度: 稀有',
  });
  requests[1].resolve(response(result()));
  await pending;
  assert.match(el('result').textContent, /已知约束兼容/);
  assert.match(el('result').textContent, /1 前缀 \+ 3 后缀/);
  assert.match(el('result').textContent, /来源 25 行；去重 23 条/);
  assert.match(el('result').textContent, /YES；原始注释：<b>原注释<\/b>/);
  assert.match(el('result').textContent, /<script>效果<\/script>/);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'SCRIPT').length, 0);
  assert.equal(nodes(el('result')).filter(node => node.tagName === 'A').length, 22,
    'untrusted host must not become a link');
  assert.match(el('result').textContent, /第 1 \/ 2 页，共 23 条/);
  assert.doesNotMatch(el('result').textContent, /词缀22/);
  const next = nodes(el('result')).find(node => node.tagName === 'BUTTON' &&
    node.textContent === '下一页');
  next.listeners.click();
  assert.match(el('result').textContent, /词缀22/);
  assert.match(el('result').textContent, /第 2 \/ 2 页，共 23 条/);
  const previous = nodes(el('result')).find(node => node.tagName === 'BUTTON' &&
    node.textContent === '上一页');
  previous.listeners.click();
  assert.match(el('result').textContent, /词缀0/);

  el('mode').value = 'manual';
  el('mode').listeners.change();
  assert.equal(el('manual').hidden, false);
  el('rarity').value = '稀有';
  el('class').value = '胸甲';
  el('prefix').value = '7';
  await context.veiledSubmit({ preventDefault() {} });
  assert.match(el('status').textContent, /0 到 6/);
  assert.equal(requests.length, 2);
  el('prefix').value = '0';
  el('suffix').value = '';
  pending = context.veiledSubmit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(requests[2].options.body), {
    operation: '加密崇高石', rarity: '稀有', item_class: '胸甲',
    prefix_count: 0, suffix_count: null, corrupted: false,
    mirrored: false, fractured: false,
  });
  requests[2].resolve(response(result({
    verdict: 'unknown', before: { ...result().before, prefix_count: 0, suffix_count: null },
  })));
  await pending;
  assert.match(el('result').textContent, /信息不足/);
  assert.match(el('result').textContent, /前缀 0；后缀 未知/);

  el('name').value = '加密混沌石';
  el('name').listeners.change();
  assert.match(el('action-note').textContent, /来源效果原文已变化/);
  pending = context.veiledSubmit({ preventDefault() {} });
  requests[3].resolve(response(result({
    verdict: 'unknown', operation: '加密混沌石',
    checks: [{ key: 'source_contract', status: 'unknown', summary: '上游变化' }],
    after: result().after,
  })));
  await pending;
  assert.match(el('result').textContent, /状态转移未知/);
  assert.doesNotMatch(el('result').textContent, /重置全部词缀/);
  assert.doesNotMatch(el('result').textContent, /移除一个随机词缀/);
  assert.match(el('result').textContent, /目录不是单次揭露选项/);

  pending = context.veiledSubmit({ preventDefault() {} });
  el('class').value = '项链';
  el('form').listeners.input();
  requests[4].resolve(response(result()));
  await pending;
  assert.equal(el('result').hidden, true, 'changed input invalidates old result');
  assert.match(el('status').textContent, /请重新核对/);

  pending = context.veiledSubmit({ preventDefault() {} });
  context.window.clearVeiledOperation();
  requests[5].resolve(response(result()));
  await pending;
  assert.equal(el('dialog').open, false);
  assert.equal(el('result').hidden, true, 'key switch invalidates old result');
  assert.equal(el('item').value, '');

  context.window.openVeiledOperation();
  assert.equal(requests[6].url, '/craft/veiled/operations');
  context.window.clearVeiledOperation();
  requests[6].resolve(response(catalog));
  await tick();
  assert.equal(el('name').children.length, 0, 'key switch invalidates old catalog');

  context.window.openVeiledOperation();
  assert.equal(requests[7].url, '/craft/veiled/operations');
  el('dialog').close();
  requests[7].resolve(response(catalog));
  await tick();
  assert.equal(el('name').children.length, 0, 'closed catalog cannot repopulate');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
