/* 独立单次加权选择模型的网页入口；不把它写成真实工艺路线。 */
'use strict';

const craftAttemptUi = {
  open: document.getElementById('craft-attempt-open'),
  dialog: document.getElementById('craft-attempt-dialog'),
  close: document.getElementById('craft-attempt-close'),
  form: document.getElementById('craft-attempt-form'),
  base: document.getElementById('craft-attempt-base'),
  level: document.getElementById('craft-attempt-level'),
  side: document.getElementById('craft-attempt-side'),
  target: document.getElementById('craft-attempt-target'),
  cap: document.getElementById('craft-attempt-cap'),
  materialRows: document.getElementById('craft-attempt-material-rows'),
  addMaterial: document.getElementById('craft-attempt-add-material'),
  price: document.getElementById('craft-attempt-price'),
  leagueWrap: document.getElementById('craft-attempt-league-wrap'),
  league: document.getElementById('craft-attempt-league'),
  run: document.getElementById('craft-attempt-run'),
  status: document.getElementById('craft-attempt-status'),
  result: document.getElementById('craft-attempt-result'),
};

const craftAttemptCategories = Object.freeze({
  auto: '自动识别', currency: '通货', scarab: '圣甲虫',
  card: '命运卡', fragment: '碎片', unique: '唯一装备',
});
let craftAttemptEpoch = 0;
let craftAttemptMaterials = [];

function craftAttemptNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function craftAttemptSection(title) {
  const section = craftAttemptNode('section', 'craft-attempt-section');
  section.append(craftAttemptNode('h3', '', title));
  return section;
}

function craftAttemptText(value, fallback = '未提供') {
  return value === null || value === undefined || value === '' ? fallback : String(value);
}

function craftAttemptSafeSource(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol === 'https:' && url.hostname === 'poedb.tw' &&
        !url.username && !url.password && !url.port) return url.href;
  } catch (_) { /* 不提供不可信外链。 */ }
  return '';
}

function craftAttemptSourceLink(raw) {
  const safe = craftAttemptSafeSource(raw);
  if (!safe) return null;
  const link = craftAttemptNode('a', '', '打开 POEDB 来源页');
  link.href = safe;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function craftAttemptList(section, title, values) {
  if (!Array.isArray(values) || !values.length) return;
  section.append(craftAttemptNode('h4', '', title));
  const list = craftAttemptNode('ul');
  for (const value of values) list.append(craftAttemptNode('li', '', value));
  section.append(list);
}

function craftAttemptTable(headers, rows) {
  const scroll = craftAttemptNode('div', 'craft-attempt-scroll');
  const table = craftAttemptNode('table', 'craft-attempt-table');
  const head = craftAttemptNode('thead');
  const headerRow = craftAttemptNode('tr');
  for (const title of headers) headerRow.append(craftAttemptNode('th', '', title));
  head.append(headerRow);
  const body = craftAttemptNode('tbody');
  for (const values of rows) {
    const row = craftAttemptNode('tr');
    for (const value of values) row.append(craftAttemptNode('td', '', value));
    body.append(row);
  }
  table.append(head, body);
  scroll.append(table);
  return scroll;
}

function craftAttemptInvalidate() {
  craftAttemptEpoch += 1;
  craftAttemptUi.run.disabled = false;
  craftAttemptUi.status.textContent = '';
  craftAttemptUi.result.hidden = true;
  craftAttemptUi.result.replaceChildren();
}

function craftAttemptInputChanged() {
  craftAttemptInvalidate();
  craftAttemptUi.status.textContent = '输入已变化；请重新计算。';
}

function craftAttemptMaterialRow(initial = {}) {
  const row = craftAttemptNode('div', 'craft-attempt-material-row');
  const nameLabel = craftAttemptNode('label', '', '材料逐字名称');
  const name = craftAttemptNode('input');
  name.maxLength = 200;
  name.placeholder = '例如：混沌石';
  name.value = initial.query || '';
  nameLabel.append(name);

  const quantityLabel = craftAttemptNode('label', '', '每次用量');
  const quantity = craftAttemptNode('input');
  quantity.type = 'number';
  quantity.min = '0';
  quantity.max = '1000000000';
  quantity.step = 'any';
  quantity.value = initial.quantity || '1';
  quantityLabel.append(quantity);

  const categoryLabel = craftAttemptNode('label', '', '类别');
  const category = craftAttemptNode('select');
  for (const [code, label] of Object.entries(craftAttemptCategories)) {
    const option = craftAttemptNode('option', '', label);
    option.value = code;
    category.append(option);
  }
  category.value = initial.category || 'auto';
  categoryLabel.append(category);

  const remove = craftAttemptNode('button', 'ghost', '移除');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    craftAttemptMaterials = craftAttemptMaterials.filter(item => item.row !== row);
    craftAttemptRenderMaterialRows();
    craftAttemptInputChanged();
  });
  row.append(nameLabel, quantityLabel, categoryLabel, remove);
  return { row, name, quantity, category, remove };
}

function craftAttemptRenderMaterialRows() {
  craftAttemptUi.materialRows.replaceChildren(...craftAttemptMaterials.map(item => item.row));
  craftAttemptUi.addMaterial.disabled = craftAttemptMaterials.length >= 20;
  for (const item of craftAttemptMaterials) item.remove.disabled = craftAttemptMaterials.length <= 1;
}

function craftAttemptAddMaterial(initial = {}) {
  if (craftAttemptMaterials.length >= 20) return;
  craftAttemptMaterials.push(craftAttemptMaterialRow(initial));
  craftAttemptRenderMaterialRows();
  craftAttemptInputChanged();
}

function craftAttemptRequiredText(node, label, maxLength) {
  const value = node.value.trim();
  if (!value || value.length > maxLength) throw new Error(`${label}必须填写，且不超过 ${maxLength} 字。`);
  return value;
}

function craftAttemptNumber(node, label, max, integer = false) {
  const raw = node.value.trim();
  const number = Number(raw);
  if (!raw || !Number.isFinite(number) || number <= 0 || number > max ||
      (integer && !Number.isInteger(number))) {
    throw new Error(`${label}必须是 0 到 ${max} 之间的${integer ? '整数' : '有限正数'}。`);
  }
  return number;
}

function craftAttemptReadRequest() {
  const base_type = craftAttemptRequiredText(craftAttemptUi.base, '胸甲底材', 80);
  const item_level = craftAttemptNumber(craftAttemptUi.level, '物品等级', 100, true);
  const generation_side = craftAttemptUi.side.value;
  if (!['prefix', 'suffix'].includes(generation_side)) throw new Error('生成侧必须是前缀或后缀。');
  const target_query = craftAttemptRequiredText(craftAttemptUi.target, '目标词缀组', 100);
  const max_attempts = craftAttemptNumber(craftAttemptUi.cap, '尝试上限', 1_000_000, true);
  if (craftAttemptMaterials.length < 1 || craftAttemptMaterials.length > 20) {
    throw new Error('每次材料清单必须包含 1 到 20 项。');
  }
  const seen = new Set();
  const materials = craftAttemptMaterials.map((item, index) => {
    const query = craftAttemptRequiredText(item.name, `第 ${index + 1} 项材料名称`, 200);
    const quantity_per_attempt = craftAttemptNumber(
      item.quantity, `第 ${index + 1} 项每次用量`, 1_000_000_000
    );
    const category = item.category.value;
    if (!Object.hasOwn(craftAttemptCategories, category)) throw new Error('材料类别无效。');
    const key = JSON.stringify([query, category]);
    if (seen.has(key)) throw new Error(`材料“${query}”重复；请先合并每次用量。`);
    seen.add(key);
    return { query, quantity_per_attempt, category };
  });
  const include_market_prices = craftAttemptUi.price.checked;
  const league = include_market_prices ? craftAttemptUi.league.value.trim() : '';
  if (league.length > 100) throw new Error('联盟名称不能超过 100 字。');
  return {
    base_type, item_level, generation_side, target_query, materials,
    max_attempts, include_market_prices, league: league || null,
  };
}

async function craftAttemptError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string') return payload.detail;
    if (typeof payload?.detail?.reason === 'string') return payload.detail.reason;
  } catch (_) { /* 非 JSON 错误以状态码说明。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

function craftAttemptRenderUnknown(data) {
  const section = craftAttemptSection('无法建立独立尝试计划');
  section.append(craftAttemptNode('p', 'craft-attempt-gap',
    '未形成完整、唯一且新鲜的普通同侧词缀池；未知不能解释成 0% 成功率。'));
  const gaps = Array.isArray(data.gaps) ? data.gaps : [];
  for (const gap of gaps) {
    section.append(craftAttemptNode('p', 'craft-attempt-gap',
      `${gap.code || '依据不足'}：${gap.message || '未提供原因'}`));
    const link = craftAttemptSourceLink(gap.authority_url);
    if (link) section.append(link);
  }
  const groups = Array.isArray(data.pool?.candidate_groups) ? data.pool.candidate_groups : [];
  if (groups.length) section.append(craftAttemptNode('h4', '', '可能的目标组：请明确选择并重新计算'));
  for (const group of groups) {
    if (!group.correct_group) continue;
    const button = craftAttemptNode('button', 'ghost',
      `${group.correct_group} · 可用阶梯 ${group.eligible_tiers ?? '未知'} · ` +
      `${(group.sample_texts || []).join('；')}`);
    button.type = 'button';
    button.addEventListener('click', () => {
      craftAttemptUi.target.value = group.correct_group;
      craftAttemptInputChanged();
      craftAttemptUi.target.focus();
    });
    section.append(button);
  }
  if (data.pricing?.reason) section.append(craftAttemptNode('p', '', `市场计价：${data.pricing.reason}`));
  craftAttemptUi.result.append(section);
}

function craftAttemptRenderPool(data) {
  const pool = data.pool || {};
  const proof = pool.page_snapshot || {};
  const section = craftAttemptSection('已验证的普通同侧词缀池');
  const side = pool.generation_side === 'prefix' ? '前缀' : '后缀';
  section.append(craftAttemptNode('p', '',
    `${pool.base?.name_zh || pool.base_type || '未知底材'} · 物等 ${pool.item_level ?? '未知'} · ` +
    `normal ${side} · 目标组 ${pool.target_group || '未知'}。`));
  section.append(craftAttemptNode('p', '',
    `池中 ${pool.pool_size ?? '未知'} 条正权重候选，目标权重 ${pool.target_weight ?? '未知'}，` +
    `池总权重 ${pool.total_weight ?? '未知'}。`));
  section.append(craftAttemptNode('p', '',
    `来源快照：${proof.source_fetched_at || '未提供抓取时间'}；范围 ${proof.scope || '未知'}；` +
    `页级行数 ${proof.row_count ?? '未知'}。`));
  if (pool.pool_digest) section.append(craftAttemptNode('p', '', `池摘要：${pool.pool_digest}`));
  const link = craftAttemptSourceLink(proof.source_final_url || proof.source_url);
  if (link) section.append(link);
  craftAttemptUi.result.append(section);
}

function craftAttemptRenderDistribution(data) {
  const distribution = data.distribution;
  const section = craftAttemptSection('单次条件概率与独立尝试分布');
  section.append(craftAttemptNode('strong', '',
    `${craftAttemptText(distribution.single_attempt_probability_numerator)} / ` +
    `${craftAttemptText(distribution.single_attempt_probability_denominator)} = ` +
    `${craftAttemptText(distribution.single_attempt_probability)}`));
  section.append(craftAttemptNode('p', '',
    '只表示已验证 normal 同侧池中恰好一次加权选择命中该组的条件概率。'));
  section.append(craftAttemptNode('p', '',
    `无限重试直到首次命中的期望次数：${craftAttemptText(distribution.expected_attempts_until_success)}。`));
  section.append(craftAttemptNode('p', '',
    `最多 ${data.max_attempts} 次：至少成功一次 ${craftAttemptText(distribution.success_probability_within_cap)}；` +
    `全部失败 ${craftAttemptText(distribution.exhaustion_probability)}；` +
    `成功即停的期望实际尝试数 ${craftAttemptText(distribution.expected_attempts_consumed_with_cap)}。`));
  const milestones = Array.isArray(distribution.milestones) ? distribution.milestones : [];
  section.append(craftAttemptTable(
    ['目标累计概率', '最少尝试数', '实际累计概率', '在设定上限内'],
    milestones.map(item => [
      craftAttemptText(item.target_probability), craftAttemptText(item.attempts),
      craftAttemptText(item.achieved_probability), item.within_attempt_cap ? '是' : '否',
    ])
  ));
  craftAttemptUi.result.append(section);
}

function craftAttemptRenderMaterials(data) {
  const section = craftAttemptSection('显式材料数量预算');
  section.append(craftAttemptNode('p', '',
    'P50/P90/P95 是达到对应累计概率所需的材料量；超出尝试上限的分位数仍会列出并明确标记。'));
  const milestones = Object.fromEntries(
    (data.distribution?.milestones || []).map(item => [item.label, item])
  );
  const rows = (Array.isArray(data.material_budget) ? data.material_budget : []).map(item => {
    const percentile = label => {
      const amount = craftAttemptText(item.milestones?.[label]);
      return milestones[label]?.within_attempt_cap ? amount : `${amount}（超出上限）`;
    };
    return [
      craftAttemptText(item.query), craftAttemptText(item.quantity_per_attempt),
      craftAttemptText(item.expected_until_success),
      craftAttemptText(item.expected_with_attempt_cap),
      craftAttemptText(item.maximum_at_attempt_cap),
      percentile('p50'), percentile('p90'), percentile('p95'),
    ];
  });
  section.append(craftAttemptTable(
    ['材料', '每次', '无限重试期望', '上限内成功即停期望', '最坏上限', 'P50', 'P90', 'P95'],
    rows
  ));
  craftAttemptUi.result.append(section);
}

function craftAttemptRenderPricing(data) {
  const section = craftAttemptSection('可选市场成本');
  const pricing = data.pricing || {};
  const cost = data.cost_budget;
  if (pricing.status === 'complete' && cost) {
    section.append(craftAttemptNode('p', '',
      `同口径快照：联盟 ${pricing.league || '未知'}；来源 ${pricing.source || '未知'}；` +
      `${pricing.snapshot_started_at || '未知'} 至 ${pricing.snapshot_finished_at || '未知'}。`));
    section.append(craftAttemptNode('p', '',
      `每次代表成本 ${craftAttemptText(cost.per_attempt_chaos)} 混沌石；` +
      `无限重试期望 ${craftAttemptText(cost.expected_until_success_chaos)}；` +
      `上限内成功即停期望 ${craftAttemptText(cost.expected_with_attempt_cap_chaos)}；` +
      `最坏上限 ${craftAttemptText(cost.maximum_at_attempt_cap_chaos)}。`));
    const milestoneScope = Object.fromEntries(
      (data.distribution?.milestones || []).map(item => [item.label, item.within_attempt_cap])
    );
    const milestoneCost = label => {
      const value = craftAttemptText(cost.milestones_chaos?.[label]);
      return milestoneScope[label] ? value : `${value}（超出上限）`;
    };
    section.append(craftAttemptNode('p', '',
      `P50 / P90 / P95 所需材料对应成本：${milestoneCost('p50')} / ` +
      `${milestoneCost('p90')} / ${milestoneCost('p95')} 混沌石。`));
    if (pricing.warning) section.append(craftAttemptNode('p', 'craft-attempt-gap', pricing.warning));
  } else if (pricing.status === 'not_requested') {
    section.append(craftAttemptNode('p', '', '本次未请求市场计价，只显示尝试次数与材料数量。'));
  } else {
    section.append(craftAttemptNode('p', 'craft-attempt-gap',
      `整张材料清单的市场总成本不可计算：${pricing.reason || '缺少完整同口径报价'}。不输出部分总价。`));
  }
  craftAttemptUi.result.append(section);
}

function craftAttemptRender(data) {
  craftAttemptUi.result.replaceChildren();
  if (data.status !== 'exact' || !data.distribution) {
    craftAttemptRenderUnknown(data);
  } else {
    craftAttemptRenderPool(data);
    craftAttemptRenderDistribution(data);
    craftAttemptRenderMaterials(data);
    craftAttemptRenderPricing(data);
  }
  const boundary = craftAttemptSection('模型假设与排除范围');
  craftAttemptList(boundary, '假设', data.assumptions);
  craftAttemptList(boundary, '不覆盖', data.exclusions);
  craftAttemptUi.result.append(boundary);
  craftAttemptUi.result.hidden = false;
}

async function craftAttemptSubmit(event) {
  event?.preventDefault();
  craftAttemptInvalidate();
  let request;
  try {
    request = craftAttemptReadRequest();
  } catch (error) {
    craftAttemptUi.status.textContent = String(error.message || error);
    return;
  }
  const epoch = craftAttemptEpoch;
  craftAttemptUi.run.disabled = true;
  craftAttemptUi.status.textContent = '正在核实词缀池并计算独立尝试预算…';
  try {
    const response = await apiFetch('/craft/attempt-plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (epoch !== craftAttemptEpoch || !craftAttemptUi.dialog.open) return;
    if (!response.ok) {
      const message = await craftAttemptError(response);
      if (epoch === craftAttemptEpoch && craftAttemptUi.dialog.open) craftAttemptUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== craftAttemptEpoch || !craftAttemptUi.dialog.open) return;
    craftAttemptRender(data);
    craftAttemptUi.status.textContent = data.status === 'exact'
      ? '计算完成；结果只适用于下方列出的独立选择假设。'
      : '依据不足；未输出概率、尝试分布或预算。';
  } catch (error) {
    if (epoch === craftAttemptEpoch && craftAttemptUi.dialog.open) {
      craftAttemptUi.status.textContent = `计算失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === craftAttemptEpoch) craftAttemptUi.run.disabled = false;
  }
}

function clearCraftAttemptWorkbench() {
  craftAttemptInvalidate();
  craftAttemptUi.form.reset();
  craftAttemptUi.base.value = '';
  craftAttemptUi.level.value = '50';
  craftAttemptUi.side.value = 'prefix';
  craftAttemptUi.target.value = '';
  craftAttemptUi.cap.value = '100';
  craftAttemptUi.price.checked = false;
  craftAttemptUi.league.value = '';
  craftAttemptUi.leagueWrap.hidden = true;
  craftAttemptMaterials = [craftAttemptMaterialRow()];
  craftAttemptRenderMaterialRows();
  if (craftAttemptUi.dialog.open) craftAttemptUi.dialog.close();
}

window.clearCraftAttemptWorkbench = clearCraftAttemptWorkbench;
craftAttemptMaterials = [craftAttemptMaterialRow()];
craftAttemptRenderMaterialRows();
craftAttemptUi.open.addEventListener('click', () => {
  if (!craftAttemptUi.dialog.open) craftAttemptUi.dialog.showModal();
  craftAttemptUi.base.focus();
});
craftAttemptUi.close.addEventListener('click', () => craftAttemptUi.dialog.close());
craftAttemptUi.dialog.addEventListener('close', () => {
  if (!craftAttemptUi.dialog.open) craftAttemptInvalidate();
});
craftAttemptUi.form.addEventListener('submit', craftAttemptSubmit);
craftAttemptUi.form.addEventListener('input', craftAttemptInputChanged);
craftAttemptUi.form.addEventListener('change', craftAttemptInputChanged);
craftAttemptUi.addMaterial.addEventListener('click', () => craftAttemptAddMaterial());
craftAttemptUi.price.addEventListener('change', () => {
  craftAttemptUi.leagueWrap.hidden = !craftAttemptUi.price.checked;
});
