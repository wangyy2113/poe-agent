// Run the browser script against a small DOM stub to exercise request and rendering behavior.
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
    this.disabled = false;
    this.hidden = false;
    this.open = false;
  }
  set textContent(value) { this.children = []; this._text = String(value); }
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
  'rare-open', 'rare-dialog', 'rare-close', 'rare-form', 'rare-text',
  'rare-league', 'rare-quality', 'rare-sockets', 'rare-submit',
  'rare-status', 'rare-result',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
const requests = [];
const context = vm.createContext({
  document: {
    getElementById: id => elements[id],
    createElement: tag => new Element(tag),
  },
  window: {},
  URL,
  Intl,
  apiFetch: (url, options) => {
    const next = deferred();
    requests.push({ url, options, next });
    return next.promise;
  },
});
const source = fs.readFileSync(
  path.resolve(__dirname, '../../static/rare-comparables.js'), 'utf8'
);
vm.runInContext(source, context);

function nodes(root) {
  return [root, ...root.children.flatMap(nodes)];
}

function response(data, status = 200) {
  return { ok: status === 200, status, json: async () => data };
}

function estimate(overrides = {}) {
  return {
    available: true, status: 'ready', reason: '在线挂单样本，不是成交价',
    league: 'Settlers', source_url: 'https://www.pathofexile.com/trade/search/Settlers/abc',
    fetched_at: '2026-10-28T08:00:00Z', base_type_input: '玛瑙护身符',
    base_type_en: 'Onyx Amulet', item_level: 84, corrupted: false,
    explicit_mod_count: 2, mapped_mod_count: 1, comparison_quality: 'partial',
    quality_filter: 20, socket_group_sizes: [2, 3],
    mapped_stats: [{ mod_text: '<img src=x onerror=alert(1)>', official_texts: ['+# to maximum Life'] }],
    unmapped_mods: ['<script>alert(1)</script>'],
    quote: {
      median_chaos: 25, min_chaos: 20, max_chaos: 35, sample_count: 6,
      minimum_sample_count: 5, search_total: 120, fetched_listing_count: 40,
      chaos_listing_count: 5, divine_listing_count: 2,
      quality_mismatch_filtered_count: 3, socket_mismatch_filtered_count: 2,
      structure_missing_filtered_count: 1, sample_scope: '按价格升序最多抓取前 40 条结果；不是成交价',
    },
    ...overrides,
  };
}

async function main() {
  context.window.openRareComparables('装备复制文本');
  assert.equal(elements['rare-dialog'].open, true);
  assert.equal(elements['rare-text'].value, '装备复制文本');
  assert.equal(requests.length, 0, 'opening or prefilling must not contact Trade');

  elements['rare-league'].value = 'Settlers';
  elements['rare-quality'].checked = true;
  elements['rare-sockets'].checked = true;
  const first = context.submitRareComparables({ preventDefault() {} });
  assert.equal(requests[0].url, '/economy/rare-comparables');
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    item_text: '装备复制文本', league: 'Settlers',
    match_quality: true, match_sockets: true,
  });
  assert.equal(elements['rare-submit'].disabled, true);
  requests[0].next.resolve(response(estimate()));
  await first;
  assert.equal(elements['rare-submit'].disabled, false);
  assert.match(elements['rare-result'].textContent, /25 混沌石/);
  assert.match(elements['rare-result'].textContent, /品质不符3/);
  assert.match(elements['rare-result'].textContent, /抓取前 40 条/);
  assert.match(elements['rare-result'].textContent, /<script>alert\(1\)<\/script>/);
  const links = nodes(elements['rare-result']).filter(node => node.tagName === 'A');
  assert.equal(links.length, 1);
  assert.equal(links[0].rel, 'noopener noreferrer');

  const second = context.submitRareComparables({ preventDefault() {} });
  requests[1].next.resolve(response(estimate({
    available: false, status: 'insufficient_samples', source_url: 'javascript:alert(1)',
    quote: { ...estimate().quote, median_chaos: null, sample_count: 2 },
  })));
  await second;
  assert.match(elements['rare-result'].textContent, /样本不足，不输出代表价/);
  assert.doesNotMatch(elements['rare-result'].textContent, /25 混沌石/);
  assert.equal(nodes(elements['rare-result']).filter(node => node.tagName === 'A').length, 0);

  const third = context.submitRareComparables({ preventDefault() {} });
  elements['rare-dialog'].close();
  context.window.openRareComparables('新装备');
  const fourth = context.submitRareComparables({ preventDefault() {} });
  elements['rare-dialog'].listeners.close(); // delayed close event from the previous opening
  assert.equal(elements['rare-submit'].disabled, true);
  requests[2].next.resolve(response(estimate()));
  await third;
  assert.equal(elements['rare-submit'].disabled, true, 'old request must not unlock new request');
  requests[3].next.resolve(response(estimate({ reason: '新结果' })));
  await fourth;
  assert.match(elements['rare-result'].textContent, /新结果/);

  const fifth = context.submitRareComparables({ preventDefault() {} });
  const errorBody = deferred();
  requests[4].next.resolve({ ok: false, status: 422, json: () => errorBody.promise });
  await new Promise(setImmediate);
  elements['rare-dialog'].close();
  context.window.openRareComparables('再试');
  const sixth = context.submitRareComparables({ preventDefault() {} });
  errorBody.resolve({ detail: { reason: '过期错误' } });
  await fifth;
  assert.doesNotMatch(elements['rare-status'].textContent, /过期错误/);
  assert.equal(elements['rare-submit'].disabled, true);
  requests[5].next.resolve(response(estimate({ quote: null, available: false })));
  await sixth;
  assert.match(elements['rare-result'].textContent, /没有可查询的官方显式 stat/);

  context.window.clearRareComparables();
  assert.equal(elements['rare-dialog'].open, false);
  assert.equal(elements['rare-text'].value, '');
  assert.equal(elements['rare-result'].textContent, '');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
