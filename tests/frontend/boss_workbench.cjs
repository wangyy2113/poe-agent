// Execute the real Boss workbench against a DOM/API stub; no browser or live DB needed.
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

const ids = [
  'boss-open', 'boss-dialog', 'boss-close', 'boss-form', 'boss-level',
  'boss-pool', 'boss-hits', 'boss-roll', 'boss-fire', 'boss-cold',
  'boss-lightning', 'boss-chaos', 'boss-armour', 'boss-physical',
  'boss-taken', 'boss-overwhelm', 'boss-uber', 'boss-run', 'boss-status',
  'boss-summary', 'boss-skills', 'boss-detail',
];
const elements = Object.fromEntries(ids.map(id => [id, new Element(id)]));
Object.assign(elements['boss-level'], { value: '84' });
Object.assign(elements['boss-hits'], { value: '1' });
Object.assign(elements['boss-physical'], { value: '0' });
Object.assign(elements['boss-taken'], { value: '1' });
Object.assign(elements['boss-overwhelm'], { value: '0' });
const requests = [];
const context = vm.createContext({
  document: { getElementById: id => elements[id], createElement: tag => new Element(tag) },
  window: {}, Number, Object,
  apiFetch: (url, options) => {
    const next = deferred();
    requests.push({ url, options, next });
    return next.promise;
  },
});
vm.runInContext(fs.readFileSync(
  path.resolve(__dirname, '../../static/boss-workbench.js'), 'utf8'
), context);

function skill(overrides = {}) {
  return {
    skill: '火球', damage_type: 'Fire', assessable: true,
    risk_rank: 1, outcome: 'roll_dependent', taken_low: 3000, taken_high: 12000,
    total_taken_low: 3000, total_taken_high: 12000,
    minimum_pool_for_full_range: 12001, pool_shortfall_for_full_range: 2001,
    required_resist_status: 'reachable', required_resist_for_full_range_pct: 82,
    resist_shortfall_pct: 7, required_armour_status: 'not_applicable',
    missing_inputs: [], reason: '', ...overrides,
  };
}
function audit(items, overrides = {}) {
  return {
    found: true, assessable: false, assessment_status: 'partial',
    total_skill_count: items.length,
    assessed_skill_count: items.filter(item => item.assessable).length,
    unknown_skill_count: items.filter(item => !item.assessable).length,
    most_dangerous_skill: '火球', most_dangerous_damage_type: 'Fire',
    most_dangerous_total_taken_low: 3000, most_dangerous_total_taken_high: 12000,
    most_dangerous_pool_ratio_pct: 120,
    minimum_pool_for_all_assessed_skills: 12001,
    pool_shortfall_for_all_assessed_skills: 2001,
    pool_bottleneck_skill: '火球', skills: items, missing_inputs: ['护甲'],
    boundary: '不代表真实 Boss 连招', ...overrides,
  };
}

async function main() {
  elements['boss-open'].listeners.click();
  assert.equal(elements['boss-dialog'].open, true);
  assert.equal(requests.length, 0, 'opening the dialog must not auto-run an audit');

  elements['boss-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests.length, 0);
  assert.match(elements['boss-status'].textContent, /请填写本次防御池/);

  elements['boss-pool'].value = '10000';
  elements['boss-fire'].value = '75';
  elements['boss-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[0].url, '/boss/audit');
  const firstBody = JSON.parse(requests[0].options.body);
  assert.equal(firstBody.level, 84);
  assert.equal(firstBody.pool, 10000);
  assert.equal(firstBody.armour, null, 'unknown armour must not become zero');
  assert.equal(firstBody.cold_resist_pct, null);
  assert.equal(firstBody.fire_resist_pct, 75);
  assert.equal(firstBody.taken_mult, 1);

  const fire = skill({ skill: '<script>alert(1)</script>' });
  const physical = skill({
    skill: '猛击', damage_type: 'Physical', assessable: false,
    risk_rank: null, reason: '护甲未知', missing_inputs: ['armour'],
  });
  requests[0].next.resolve(response(audit([fire, physical])));
  await tick();
  assert.match(elements['boss-summary'].textContent, /可评估 1 条；未知 1 条/);
  assert.match(elements['boss-summary'].textContent, /护甲 未知/);
  assert.match(elements['boss-summary'].textContent, /不能把已知最危险项/);
  assert.match(elements['boss-skills'].textContent, /未评估项（不参与风险排名）/);
  assert.match(elements['boss-skills'].textContent, /<script>alert\(1\)<\/script>/);
  const buttons = nodes(elements['boss-skills']).filter(node => node.tagName === 'BUTTON');
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, true, 'physical threshold needs explicit armour');

  buttons[0].listeners.click();
  assert.equal(requests[1].url, '/boss/threshold');
  const thresholdBody = JSON.parse(requests[1].options.body);
  assert.equal(thresholdBody.target_pool, 10000);
  assert.equal(thresholdBody.skill, fire.skill);
  assert.equal(Object.hasOwn(thresholdBody, 'pool'), false);
  assert.equal(Object.hasOwn(thresholdBody, 'armour'), false,
    'elemental threshold may omit unknown armour');
  requests[1].next.resolve(response({
    found: true, assessable: true, skill: fire.skill, target_pool: 10000,
    taken_low: 3000, taken_high: 12000, total_taken_low: 3000,
    total_taken_high: 12000, minimum_pool_for_full_range: 12001,
    pool_shortfall_for_full_range: 2001, required_resist_status: 'reachable',
    required_resist_for_full_range_pct: 82, resist_shortfall_pct: 7,
    required_armour_status: 'not_applicable', boundary: '仅同一技能重复命中',
  }));
  await tick();
  assert.match(elements['boss-detail'].textContent, /还差 7 个百分点/);
  assert.match(elements['boss-detail'].textContent, /仅同一技能重复命中/);

  const oldDetail = context.bossLoadThreshold(fire.skill, 'Fire');
  elements['boss-pool'].value = '13000';
  elements['boss-form'].listeners.input();
  assert.equal(elements['boss-detail'].hidden, true);
  assert.equal(elements['boss-skills'].textContent, '');
  elements['boss-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[3].url, '/boss/audit');
  assert.equal(JSON.parse(requests[3].options.body).pool, 13000);
  requests[2].next.resolve(response({
    found: true, assessable: true, skill: fire.skill,
    target_pool: 10000, boundary: '过期门槛',
  }));
  await oldDetail;
  assert.doesNotMatch(elements['boss-detail'].textContent, /过期门槛/);
  requests[3].next.resolve(response(audit([skill()], {
    assessment_status: 'complete', unknown_skill_count: 0, missing_inputs: [],
  })));
  await tick();
  assert.match(elements['boss-summary'].textContent, /已完整评估/);

  elements['boss-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests[4].url, '/boss/audit');
  context.window.clearBossWorkbench();
  assert.equal(elements['boss-dialog'].open, false);
  assert.equal(elements['boss-pool'].value, '');
  assert.equal(elements['boss-summary'].textContent, '');
  requests[4].next.resolve(response(audit([skill()], { boundary: '过期审计' })));
  await tick();
  assert.doesNotMatch(elements['boss-summary'].textContent, /过期审计/);

  elements['boss-open'].listeners.click();
  elements['boss-pool'].value = '10000';
  elements['boss-hits'].value = '1.5';
  elements['boss-form'].listeners.submit({ preventDefault() {} });
  assert.equal(requests.length, 5);
  assert.match(elements['boss-status'].textContent, /整数/);
  elements['boss-hits'].value = '1';
  elements['boss-armour'].value = '0';
  elements['boss-form'].listeners.submit({ preventDefault() {} });
  assert.equal(JSON.parse(requests[5].options.body).armour, 0,
    'an explicit zero armour value remains distinct from unknown');
  requests[5].next.resolve(response(audit([skill({
    skill: '猛击', damage_type: 'Physical', required_resist_status: 'not_applicable',
    required_armour_status: 'reachable', required_armour_for_full_range: 20000,
    armour_shortfall: 20000,
  })], { assessment_status: 'complete', unknown_skill_count: 0, missing_inputs: [] })));
  await tick();
  const physicalButton = nodes(elements['boss-skills'])
    .find(node => node.tagName === 'BUTTON');
  assert.equal(physicalButton.disabled, false);
  physicalButton.listeners.click();
  assert.equal(JSON.parse(requests[6].options.body).armour, 0);
  requests[6].next.resolve(response({
    found: false, assessable: false, reason: '该技能当前无可用模型',
    missing_inputs: ['damage_range'], boundary: '未知不能按安全处理',
  }));
  await tick();
  assert.match(elements['boss-detail'].textContent, /当前无可用模型/);
  assert.match(elements['boss-detail'].textContent, /damage_range/);

  elements['boss-form'].listeners.submit({ preventDefault() {} });
  requests[7].next.resolve(response({ detail: { reason: '场景超出模型范围' } }, 422));
  await tick();
  assert.match(elements['boss-status'].textContent, /场景超出模型范围/);
  assert.equal(elements['boss-summary'].hidden, true);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
