// Run the real independent-attempt workbench against an isolated DOM/API stub.
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

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function response(data, status = 200) {
  return { ok: status === 200, status, json: async () => data };
}
function tick() { return new Promise(resolve => setImmediate(resolve)); }
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function row(index) {
  const root = elements['craft-attempt-material-rows'].children[index];
  return {
    name: root.children[0].children[0],
    quantity: root.children[1].children[0],
    category: root.children[2].children[0],
    remove: root.children[3],
  };
}

const ids = [
  'craft-attempt-open', 'craft-attempt-dialog', 'craft-attempt-close',
  'craft-attempt-form', 'craft-attempt-base', 'craft-attempt-level',
  'craft-attempt-side', 'craft-attempt-target', 'craft-attempt-cap',
  'craft-attempt-material-rows', 'craft-attempt-add-material',
  'craft-attempt-price', 'craft-attempt-league-wrap', 'craft-attempt-league',
  'craft-attempt-run', 'craft-attempt-status', 'craft-attempt-result',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
elements['craft-attempt-level'].value = '50';
elements['craft-attempt-side'].value = 'prefix';
elements['craft-attempt-cap'].value = '100';
const requests = [];
const context = vm.createContext({
  document: { getElementById: id => elements[id], createElement: tag => new Element(tag) },
  window: {}, URL, Object,
  apiFetch: (url, options) => {
    const next = deferred();
    requests.push({ url, options, next });
    return next.promise;
  },
});
vm.runInContext(fs.readFileSync(
  path.resolve(__dirname, '../../static/craft-attempt-workbench.js'), 'utf8'
), context);

function exact(overrides = {}) {
  return {
    status: 'exact', max_attempts: 100,
    pool: {
      base_type: '铁制背心', base: { name_zh: '<script>铁制背心</script>' },
      item_level: 50, generation_side: 'prefix', target_group: 'IncreasedLife',
      pool_size: 20, target_weight: 300, total_weight: 1000,
      pool_digest: 'pool-digest', page_snapshot: {
        source_fetched_at: '2026-10-28T00:00:00Z', scope: 'normal', row_count: 20,
        source_final_url: 'https://poedb.tw/cn/Body_Armours#ModifiersCalc',
      },
    },
    distribution: {
      model: 'geometric_iid_single_weighted_selection_v1',
      single_attempt_probability_numerator: 3,
      single_attempt_probability_denominator: 10,
      single_attempt_probability: '0.3',
      expected_attempts_until_success: '3.33333333333333333',
      attempt_cap: 100, success_probability_within_cap: '0.999999999',
      exhaustion_probability: '0.000000001',
      expected_attempts_consumed_with_cap: '3.33333333',
      milestones: [
        { label: 'p50', target_probability: '0.5', attempts: 2,
          achieved_probability: '0.51', within_attempt_cap: true },
        { label: 'p90', target_probability: '0.9', attempts: 7,
          achieved_probability: '0.9176457', within_attempt_cap: true },
        { label: 'p95', target_probability: '0.95', attempts: 120,
          achieved_probability: '0.95', within_attempt_cap: false },
      ],
    },
    material_budget: [
      { query: '混沌石', quantity_per_attempt: '1',
        expected_until_success: '3.33333333333333333',
        expected_with_attempt_cap: '3.33333333', maximum_at_attempt_cap: '100',
        milestones: { p50: '2', p90: '7', p95: '120' } },
      { query: '增幅石', quantity_per_attempt: '0.5',
        expected_until_success: '1.66666666666666667',
        expected_with_attempt_cap: '1.66666667', maximum_at_attempt_cap: '50',
        milestones: { p50: '1', p90: '3.5', p95: '60' } },
    ],
    pricing: { status: 'incomplete', reason: '一项材料缺价' },
    cost_budget: null,
    assumptions: ['每次都完全重置'],
    exclusions: ['不是混沌石一次使用的成功率'],
    gaps: [], ...overrides,
  };
}

async function main() {
  assert.equal(elements['craft-attempt-material-rows'].children.length, 1);
  assert.equal(row(0).remove.disabled, true);
  elements['craft-attempt-open'].listeners.click();
  assert.equal(elements['craft-attempt-dialog'].open, true);
  assert.equal(requests.length, 0, 'opening must never auto-submit a plan');

  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests.length, 0);
  assert.match(elements['craft-attempt-status'].textContent, /胸甲底材必须填写/);

  elements['craft-attempt-base'].value = '铁制背心';
  elements['craft-attempt-target'].value = 'IncreasedLife';
  row(0).name.value = '混沌石';
  row(0).category.value = 'currency';
  elements['craft-attempt-add-material'].listeners.click();
  assert.equal(elements['craft-attempt-material-rows'].children.length, 2);
  row(1).name.value = '混沌石';
  row(1).category.value = 'currency';
  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests.length, 0);
  assert.match(elements['craft-attempt-status'].textContent, /重复/);

  row(1).name.value = '增幅石';
  row(1).quantity.value = '0.5';
  elements['craft-attempt-price'].checked = true;
  elements['craft-attempt-price'].listeners.change();
  elements['craft-attempt-league'].value = 'Standard';
  elements['craft-attempt-form'].listeners.change();
  assert.equal(elements['craft-attempt-league-wrap'].hidden, false);
  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[0].url, '/craft/attempt-plan');
  const firstBody = JSON.parse(requests[0].options.body);
  assert.deepEqual(firstBody, {
    base_type: '铁制背心', item_level: 50, generation_side: 'prefix',
    target_query: 'IncreasedLife', max_attempts: 100, include_market_prices: true,
    league: 'Standard', materials: [
      { query: '混沌石', quantity_per_attempt: 1, category: 'currency' },
      { query: '增幅石', quantity_per_attempt: 0.5, category: 'currency' },
    ],
  });
  requests[0].next.resolve(response(exact()));
  await tick();
  assert.match(elements['craft-attempt-result'].textContent, /3 \/ 10 = 0\.3/);
  assert.match(elements['craft-attempt-result'].textContent, /120（超出上限）/);
  assert.match(elements['craft-attempt-result'].textContent, /整张材料清单的市场总成本不可计算/);
  assert.doesNotMatch(elements['craft-attempt-result'].textContent, /每次代表成本/);
  assert.match(elements['craft-attempt-result'].textContent, /<script>铁制背心<\/script>/);
  const links = nodes(elements['craft-attempt-result']).filter(node => node.tagName === 'A');
  assert.equal(links.length, 1);
  assert.equal(links[0].rel, 'noopener noreferrer');

  elements['craft-attempt-target'].value = '生命';
  elements['craft-attempt-form'].listeners.input();
  assert.equal(elements['craft-attempt-result'].hidden, true);
  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[1].url, '/craft/attempt-plan');
  elements['craft-attempt-target'].value = '生命词缀';
  elements['craft-attempt-form'].listeners.input();
  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[2].url, '/craft/attempt-plan');
  requests[1].next.resolve(response(exact({ pool: { target_group: '过期结果' } })));
  await tick();
  assert.doesNotMatch(elements['craft-attempt-result'].textContent, /过期结果/);
  requests[2].next.resolve(response({
    status: 'unknown', pool: { candidate_groups: [{
      correct_group: 'IncreasedLife', eligible_tiers: 3,
      sample_texts: ['<img src=x onerror=alert(1)>'],
    }] },
    gaps: [{ code: 'target_ambiguous', message: '目标不唯一',
      authority_url: 'javascript:alert(1)' }],
    pricing: { status: 'incomplete', reason: '词缀池未知，未发起市场请求' },
    assumptions: ['必须完整重置'], exclusions: ['不推断工艺步骤'],
  }));
  await tick();
  assert.match(elements['craft-attempt-result'].textContent, /未知不能解释成 0%/);
  assert.match(elements['craft-attempt-result'].textContent, /目标不唯一/);
  assert.doesNotMatch(elements['craft-attempt-result'].textContent, /单次条件概率与独立尝试分布/);
  assert.equal(nodes(elements['craft-attempt-result']).filter(node => node.tagName === 'A').length, 0);
  const candidate = nodes(elements['craft-attempt-result']).find(node => node.tagName === 'BUTTON');
  assert.match(candidate.textContent, /<img src=x onerror=alert\(1\)>/);
  candidate.listeners.click();
  assert.equal(elements['craft-attempt-target'].value, 'IncreasedLife');
  assert.equal(elements['craft-attempt-result'].hidden, true);
  assert.equal(requests.length, 3, 'candidate choice must not auto-submit');

  elements['craft-attempt-price'].checked = false;
  elements['craft-attempt-price'].listeners.change();
  elements['craft-attempt-form'].listeners.change();
  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(JSON.parse(requests[3].options.body).league, null);
  requests[3].next.resolve(response(exact({
    pricing: { status: 'not_requested', reason: '未请求' },
  })));
  await tick();
  assert.match(elements['craft-attempt-result'].textContent, /未请求市场计价/);

  elements['craft-attempt-price'].checked = true;
  elements['craft-attempt-price'].listeners.change();
  elements['craft-attempt-form'].listeners.change();
  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[4].url, '/craft/attempt-plan');
  assert.equal(JSON.parse(requests[4].options.body).include_market_prices, true);
  requests[4].next.resolve(response(exact({
    pricing: {
      status: 'complete', league: 'Standard', source: 'poe.watch',
      snapshot_started_at: '2026-10-28T00:00:00Z',
      snapshot_finished_at: '2026-10-28T00:03:00Z', warning: '仅为统计快照',
    },
    cost_budget: {
      per_attempt_chaos: '2', expected_until_success_chaos: '6.66666666666666667',
      expected_with_attempt_cap_chaos: '6.66', maximum_at_attempt_cap_chaos: '200',
      milestones_chaos: { p50: '4', p90: '14', p95: '240' },
    },
  })));
  await tick();
  assert.match(elements['craft-attempt-result'].textContent, /每次代表成本 2 混沌石/);
  assert.match(elements['craft-attempt-result'].textContent, /240（超出上限）/);
  assert.match(elements['craft-attempt-result'].textContent, /仅为统计快照/);

  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[5].url, '/craft/attempt-plan');
  requests[5].next.resolve(response({ detail: { reason: '材料参数无效' } }, 422));
  await tick();
  assert.match(elements['craft-attempt-status'].textContent, /材料参数无效/);
  assert.equal(elements['craft-attempt-result'].hidden, true);

  elements['craft-attempt-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[6].url, '/craft/attempt-plan');
  context.window.clearCraftAttemptWorkbench();
  assert.equal(elements['craft-attempt-dialog'].open, false);
  assert.equal(elements['craft-attempt-base'].value, '');
  assert.equal(elements['craft-attempt-material-rows'].children.length, 1);
  requests[6].next.resolve(response(exact({ assumptions: ['过期结果'] })));
  await tick();
  assert.doesNotMatch(elements['craft-attempt-result'].textContent, /过期结果/);
  elements['craft-attempt-add-material'].listeners.click();
  assert.equal(elements['craft-attempt-material-rows'].children.length, 2);
  row(1).remove.listeners.click();
  assert.equal(elements['craft-attempt-material-rows'].children.length, 1);
  for (let index = 1; index < 20; index += 1) {
    elements['craft-attempt-add-material'].listeners.click();
  }
  assert.equal(elements['craft-attempt-material-rows'].children.length, 20);
  assert.equal(elements['craft-attempt-add-material'].disabled, true);
  elements['craft-attempt-add-material'].listeners.click();
  assert.equal(elements['craft-attempt-material-rows'].children.length, 20);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
