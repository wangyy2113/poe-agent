// Execute the native browser module against a DOM stub; no network or build tool required.
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
    this.disabled = false;
    this.hidden = false;
    this.open = false;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('untrusted HTML injection'); }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this._text = ''; this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute(name, value) { this[name] = value; }
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
  'economy-open', 'economy-dialog', 'economy-close', 'economy-form',
  'economy-mode', 'economy-query-fields', 'economy-query', 'economy-category',
  'economy-league', 'economy-trend-fields', 'economy-window',
  'economy-material-fields', 'economy-material-rows', 'economy-add-material',
  'economy-flip-fields', 'economy-submit', 'economy-status', 'economy-result',
  'economy-quantity', 'economy-age', 'economy-buy-fixed', 'economy-buy-percent',
  'economy-sell-fixed', 'economy-sell-percent', 'economy-min-profit', 'economy-min-margin',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
Object.assign(elements['economy-mode'], { value: 'price' });
Object.assign(elements['economy-category'], { value: 'auto' });
Object.assign(elements['economy-window'], { value: '168' });
for (const id of ['economy-quantity', 'economy-age', 'economy-buy-fixed',
  'economy-buy-percent', 'economy-sell-fixed', 'economy-sell-percent',
  'economy-min-profit', 'economy-min-margin']) elements[id].value = '0';
elements['economy-quantity'].value = '1';
elements['economy-age'].value = '24';
const requests = [];
const context = vm.createContext({
  document: {
    getElementById: id => elements[id],
    createElement: tag => new Element(tag),
  },
  window: {}, URL, Intl,
  apiFetch: (url, options) => {
    const next = deferred();
    requests.push({ url, options, next });
    return next.promise;
  },
});
vm.runInContext(fs.readFileSync(
  path.resolve(__dirname, '../../static/economy-workbench.js'), 'utf8'
), context);

function response(data, status = 200) {
  return { ok: status === 200, status, json: async () => data };
}
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function submit() { return context.submitEconomy({ preventDefault() {} }); }
function setMode(mode) {
  elements['economy-mode'].value = mode;
  elements['economy-mode'].listeners.change();
}
function base(extra = {}) {
  return {
    league: 'Standard', source: 'poe.watch', source_url: 'https://poe.watch/',
    fetched_at: '2026-10-28T10:00:00Z', from_cache: false, stale: false,
    degraded: false, fallback: false, warning: '', fallback_reason: '',
    ...extra,
  };
}

async function main() {
  assert.equal(elements['economy-material-rows'].children.length, 1);
  elements['economy-open'].listeners.click();
  assert.equal(elements['economy-dialog'].open, true);
  assert.equal(requests.length, 0, 'opening must not query a remote source');

  elements['economy-query'].value = '神圣石';
  elements['economy-league'].value = 'Standard';
  const price = submit();
  assert.equal(requests[0].url, '/economy/price');
  assert.deepEqual(JSON.parse(requests[0].options.body), {
    query: '神圣石', category: 'auto', league: 'Standard',
  });
  requests[0].next.resolve(response(base({
    source_url: 'javascript:alert(1)', fallback: true, fallback_reason: '主源不可用',
    prices: [{
      name: '<img src=x onerror=alert(1)>', category: 'currency',
      representative_chaos: 12, price_basis: 'GGG 挂单中位数',
      mean_chaos: null, median_chaos: 12, min_chaos: 10, max_chaos: 14,
      low_confidence: false, daily: null, sample_count: 6, minimum_sample_count: 5,
      search_total: 100, fetched_listing_count: 40, cross_league_filtered_count: 2,
      sample_scope: '前 40 条在线挂单',
    }],
  })));
  await price;
  assert.match(elements['economy-result'].textContent, /12 混沌石/);
  assert.match(elements['economy-result'].textContent, /主源不可用/);
  assert.match(elements['economy-result'].textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(nodes(elements['economy-result']).filter(node => node.tagName === 'A').length, 0);

  setMode('trend');
  assert.equal(elements['economy-trend-fields'].hidden, false);
  const trend = submit();
  assert.equal(requests[1].url, '/economy/trend');
  assert.equal(JSON.parse(requests[1].options.body).window_hours, 168);
  requests[1].next.resolve(response(base({
    window_hours: 168, minimum_observations: 2,
    trends: [{ name: '神圣石', status: 'insufficient_observations',
      observation_count: 1, basis: '本机快照', timestamps_available: true,
      change_chaos: 999, change_percent: 999, reason: '只有一个有效观测' }],
  })));
  await trend;
  assert.match(elements['economy-result'].textContent, /观测不足，不判断趋势/);
  assert.doesNotMatch(elements['economy-result'].textContent, /999/);

  setMode('materials');
  assert.equal(elements['economy-query-fields'].hidden, true);
  const firstRow = elements['economy-material-rows'].children[0];
  firstRow.nameInput.value = '神圣石';
  firstRow.quantityInput.value = '2';
  elements['economy-add-material'].listeners.click();
  const secondRow = elements['economy-material-rows'].children[1];
  secondRow.nameInput.value = '混沌石';
  secondRow.quantityInput.value = '100';
  const materials = submit();
  assert.equal(requests[2].url, '/economy/material-cost');
  assert.deepEqual(JSON.parse(requests[2].options.body).materials, [
    { query: '神圣石', quantity: 2, category: 'auto' },
    { query: '混沌石', quantity: 100, category: 'auto' },
  ]);
  requests[2].next.resolve(response(base({
    calculable: false, unresolved_count: 1, total_representative_chaos: 999,
    materials: [{ query: '神圣石', quantity: 2, status: 'not_found',
      reason: '未找到', resolved_name: '', unit_price: null,
      subtotal_representative_chaos: null }],
  })));
  await materials;
  assert.match(elements['economy-result'].textContent, /不输出部分总价/);
  assert.doesNotMatch(elements['economy-result'].textContent, /999/);

  setMode('flip');
  elements['economy-category'].value = 'unique';
  await submit();
  assert.equal(requests.length, 3, 'unique item cannot enter exchange analysis');
  assert.match(elements['economy-status'].textContent, /唯一装备/);
  elements['economy-category'].value = 'currency';
  elements['economy-quantity'].value = '10';
  elements['economy-buy-fixed'].value = '2';
  elements['economy-buy-percent'].value = '100';
  await submit();
  assert.equal(requests.length, 3, 'a 100% fee is invalid');
  elements['economy-buy-percent'].value = '0';
  elements['economy-quantity'].value = '0';
  await submit();
  assert.equal(requests.length, 3, 'zero quantity is invalid');
  elements['economy-quantity'].value = '10';
  const flip = submit();
  assert.equal(requests[3].url, '/economy/flip-analysis');
  assert.deepEqual(JSON.parse(requests[3].options.body).fees, {
    buy_fixed_chaos: 2, buy_percent: 0, sell_fixed_chaos: 0, sell_percent: 0,
  });
  requests[3].next.resolve(response(base({
    calculable: false, verdict: 'unknown', net_profit_chaos: 999,
    reason: '卖出侧样本不足', resolved_name: 'Divine Orb', quantity: 10,
    max_listing_age_hours: 24, minimum_net_profit_chaos: 0, minimum_margin_percent: 0,
    fees: { buy_fixed_chaos: 2, buy_percent: 0, sell_fixed_chaos: 0, sell_percent: 0 },
    buy_side: { sample_count: 5, minimum_sample_count: 5, unit_chaos: 12 },
    sell_side: { sample_count: 1, minimum_sample_count: 5, unit_chaos: null },
  })));
  await flip;
  assert.match(elements['economy-result'].textContent, /利润与判断未知/);
  assert.match(elements['economy-result'].textContent, /卖出侧样本不足/);
  assert.doesNotMatch(elements['economy-result'].textContent, /999/);

  const old = submit();
  elements['economy-dialog'].close();
  elements['economy-open'].listeners.click();
  const current = submit();
  elements['economy-dialog'].listeners.close(); // delayed event from previous opening
  assert.equal(elements['economy-submit'].disabled, true);
  requests[4].next.resolve(response(base({ calculable: false, reason: '过期' })));
  await old;
  assert.equal(elements['economy-submit'].disabled, true);
  requests[5].next.resolve(response(base({
    calculable: true, verdict: 'flip_candidate', net_profit_chaos: 40,
    margin_percent: 25, resolved_name: 'Divine Orb', quantity: 10,
    max_listing_age_hours: 24, minimum_net_profit_chaos: 0, minimum_margin_percent: 0,
    total_buy_cost_chaos: 120, net_sell_revenue_chaos: 160,
    buy_fee_chaos: 2, sell_fee_chaos: 0,
    fees: { buy_fixed_chaos: 2, buy_percent: 0, sell_fixed_chaos: 0, sell_percent: 0 },
    buy_side: { sample_count: 5, minimum_sample_count: 5 },
    sell_side: { sample_count: 5, minimum_sample_count: 5 },
  })));
  await current;
  assert.match(elements['economy-result'].textContent, /40 混沌石/);
  assert.match(elements['economy-result'].textContent, /达到调用方阈值/);
  assert.doesNotMatch(elements['economy-result'].textContent, /过期/);
  const sourceLinks = nodes(elements['economy-result']).filter(node => node.tagName === 'A');
  assert.equal(sourceLinks.length, 1);
  assert.equal(sourceLinks[0].href, 'https://poe.watch/');
  assert.equal(sourceLinks[0].rel, 'noopener noreferrer');

  setMode('price');
  const unavailable = submit();
  requests[6].next.resolve(response({ detail: { reason: '主源暂不可用' } }, 503));
  await unavailable;
  assert.match(elements['economy-status'].textContent, /主源暂不可用/);
  assert.equal(elements['economy-result'].textContent, '');

  context.window.clearEconomyWorkbench();
  assert.equal(elements['economy-dialog'].open, false);
  assert.equal(elements['economy-query'].value, '');
  assert.equal(elements['economy-result'].textContent, '');
  assert.equal(elements['economy-material-rows'].children.length, 1);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
