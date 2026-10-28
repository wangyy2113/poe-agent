/* Boss 命中风险实验台：只使用用户本次显式输入和已收录的技能预设。 */
'use strict';

const bossUi = {
  open: document.getElementById('boss-open'),
  dialog: document.getElementById('boss-dialog'),
  close: document.getElementById('boss-close'),
  form: document.getElementById('boss-form'),
  level: document.getElementById('boss-level'),
  pool: document.getElementById('boss-pool'),
  hits: document.getElementById('boss-hits'),
  roll: document.getElementById('boss-roll'),
  fire: document.getElementById('boss-fire'),
  cold: document.getElementById('boss-cold'),
  lightning: document.getElementById('boss-lightning'),
  chaos: document.getElementById('boss-chaos'),
  armour: document.getElementById('boss-armour'),
  physical: document.getElementById('boss-physical'),
  taken: document.getElementById('boss-taken'),
  overwhelm: document.getElementById('boss-overwhelm'),
  uber: document.getElementById('boss-uber'),
  run: document.getElementById('boss-run'),
  status: document.getElementById('boss-status'),
  summary: document.getElementById('boss-summary'),
  skills: document.getElementById('boss-skills'),
  detail: document.getElementById('boss-detail'),
};

const bossDamageLabels = Object.freeze({
  Physical: '物理', Fire: '火焰', Cold: '冰霜', Lightning: '闪电', Chaos: '混沌',
});
const bossOutcomeLabels = Object.freeze({
  survives_full_range: '完整掷骰区间不耗尽',
  roll_dependent: '取决于掷骰',
  fails_full_range: '完整掷骰区间耗尽',
});
const bossStatusLabels = Object.freeze({
  complete: '已完整评估当前收录的可比较技能',
  partial: '仅完成部分技能评估',
  none: '没有技能可安全评估',
});

let bossAuditEpoch = 0;
let bossDetailEpoch = 0;
let bossScenario = null;

function bossNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function bossNumber(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '未知';
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
}

function bossPercent(value) {
  return value === null || value === undefined ? '未知' : `${bossNumber(value)}%`;
}

function bossRange(low, high) {
  return low === null || high === null || low === undefined || high === undefined
    ? '未知' : `${bossNumber(low)} – ${bossNumber(high)}`;
}

function bossField(node, name, min, max, options = {}) {
  const raw = node.value.trim();
  if (!raw) {
    if (options.optional) return null;
    throw new Error(`请填写${name}。`);
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value < min || value > max ||
      (options.integer && !Number.isInteger(value)) || (options.positive && value <= 0)) {
    const span = options.positive
      ? (max === Number.MAX_VALUE ? '大于 0 的有限数值' : `大于 0 且不超过 ${max}`)
      : `在 ${min} 到 ${max} 之间`;
    throw new Error(`${name}必须是${span}${options.integer ? '，且为整数' : ''}。`);
  }
  return value;
}

function bossReadScenario() {
  return {
    level: bossField(bossUi.level, '敌人等级', 1, 100, { integer: true }),
    pool: bossField(bossUi.pool, '本次防御池', 0, 1_000_000_000, { positive: true }),
    hit_count: bossField(bossUi.hits, '连续命中次数', 1, 100, { integer: true }),
    uber: bossUi.uber.checked,
    roll_range: bossField(bossUi.roll, '固定掷骰点', 0, 100, { optional: true }),
    fire_resist_pct: bossField(bossUi.fire, '火焰抗性', -200, 90, { optional: true }),
    cold_resist_pct: bossField(bossUi.cold, '冰霜抗性', -200, 90, { optional: true }),
    lightning_resist_pct: bossField(bossUi.lightning, '闪电抗性', -200, 90, { optional: true }),
    chaos_resist_pct: bossField(bossUi.chaos, '混沌抗性', -200, 90, { optional: true }),
    armour: bossField(bossUi.armour, '护甲', 0, 10_000_000_000, { optional: true }),
    flat_physical_reduction_pct: bossField(bossUi.physical, '额外固定物理减伤', 0, 90),
    taken_mult: bossField(bossUi.taken, '承受伤害倍率', 0, Number.MAX_VALUE, { positive: true }),
    overwhelm_pct: bossField(bossUi.overwhelm, '物理减伤压制', 0, 100),
  };
}

async function bossError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string') return payload.detail;
    if (typeof payload?.detail?.reason === 'string') return payload.detail.reason;
  } catch (_) { /* 非 JSON 错误使用状态码。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

function bossClearDetail() {
  bossDetailEpoch += 1;
  bossUi.detail.replaceChildren();
  bossUi.detail.hidden = true;
}

function bossClearResults() {
  bossAuditEpoch += 1;
  bossClearDetail();
  bossScenario = null;
  bossUi.summary.replaceChildren();
  bossUi.summary.hidden = true;
  bossUi.skills.replaceChildren();
  bossUi.status.textContent = '';
  bossUi.run.disabled = false;
}

function bossInputsChanged() {
  bossClearResults();
  bossUi.status.textContent = '输入已变化；请重新审计。';
}

function bossSummaryLine(textValue) {
  return bossNode('p', '', textValue);
}

function bossRenderSummary(data, scenario) {
  const target = bossUi.summary;
  target.replaceChildren();
  target.append(bossNode('h3', '', bossStatusLabels[data.assessment_status] || '审计状态未知'));
  target.append(bossSummaryLine(
    `口径：敌人等级 ${scenario.level}；同一技能连续 ${scenario.hit_count} 次；防御池 ${bossNumber(scenario.pool)}；` +
    `${scenario.uber ? 'Uber' : '普通'} 穿透预设；` +
    `${scenario.roll_range === null ? '完整掷骰区间' : `固定 ${bossNumber(scenario.roll_range)}% 掷骰点`}。`
  ));
  target.append(bossSummaryLine(
    `防御假设：火 ${bossPercent(scenario.fire_resist_pct)}、冰 ${bossPercent(scenario.cold_resist_pct)}、` +
    `雷 ${bossPercent(scenario.lightning_resist_pct)}、混沌 ${bossPercent(scenario.chaos_resist_pct)} 抗性；` +
    `护甲 ${bossNumber(scenario.armour)}；额外固定物理减伤 ${bossNumber(scenario.flat_physical_reduction_pct)}%；` +
    `承伤倍率 ${bossNumber(scenario.taken_mult)}；物理减伤压制 ${bossNumber(scenario.overwhelm_pct)}%。`
  ));
  target.append(bossSummaryLine(
    `当前收录 ${data.total_skill_count ?? 0} 条技能预设；可评估 ${data.assessed_skill_count ?? 0} 条；` +
    `未知 ${data.unknown_skill_count ?? 0} 条。`
  ));
  if (data.unknown_skill_count) {
    target.append(bossSummaryLine('存在未知项，不能把已知最危险项或已评估项门槛当作全部技能结论。'));
  }
  if (data.most_dangerous_skill) {
    target.append(bossSummaryLine(
      `已知项最危险：${data.most_dangerous_skill}（${bossDamageLabels[data.most_dangerous_damage_type] || data.most_dangerous_damage_type || '类型未知'}）；` +
      `累计承伤 ${bossRange(data.most_dangerous_total_taken_low, data.most_dangerous_total_taken_high)}；` +
      `最坏占池子 ${bossNumber(data.most_dangerous_pool_ratio_pct)}%。`
    ));
  }
  if (data.minimum_pool_for_all_assessed_skills !== null &&
      data.minimum_pool_for_all_assessed_skills !== undefined) {
    target.append(bossSummaryLine(
      `仅对已评估项的统一池子门槛：${bossNumber(data.minimum_pool_for_all_assessed_skills)}；` +
      `当前缺口 ${bossNumber(data.pool_shortfall_for_all_assessed_skills)}；` +
      `瓶颈技能 ${data.pool_bottleneck_skill || '未知'}。`
    ));
  }
  if (data.missing_inputs?.length) {
    target.append(bossSummaryLine(`缺失输入：${data.missing_inputs.join('、')}。`));
  }
  if (data.reason) target.append(bossSummaryLine(data.reason));
  if (data.boundary) target.append(bossSummaryLine(`模型边界：${data.boundary}`));
  target.hidden = false;
}

function bossSkillCard(item, scenario) {
  const card = bossNode('article', `boss-skill${item.assessable ? '' : ' unknown'}`);
  const rank = item.assessable && item.risk_rank ? `已知风险第 ${item.risk_rank} 位 · ` : '未评估 · ';
  card.append(bossNode('h3', '', `${rank}${item.skill || '未知技能'}`));
  card.append(bossSummaryLine(
    `伤害类型：${bossDamageLabels[item.damage_type] || item.damage_type || '未知'}；` +
    `${item.assessable ? bossOutcomeLabels[item.outcome] || '结论未知' : '缺少安全评估依据'}。`
  ));
  if (item.assessable) {
    card.append(bossSummaryLine(
      `每击承伤 ${bossRange(item.taken_low, item.taken_high)}；` +
      `累计承伤 ${bossRange(item.total_taken_low, item.total_taken_high)}。`
    ));
    card.append(bossSummaryLine(
      `覆盖当前掷骰范围所需池子 ${bossNumber(item.minimum_pool_for_full_range)}；` +
      `当前缺口 ${bossNumber(item.pool_shortfall_for_full_range)}。`
    ));
    if (item.required_resist_status === 'reachable') {
      card.append(bossSummaryLine(
        `只补对应抗性：目标 ${bossNumber(item.required_resist_for_full_range_pct)}%，` +
        `差 ${bossNumber(item.resist_shortfall_pct)} 个百分点。`
      ));
    } else if (item.required_armour_status === 'reachable') {
      card.append(bossSummaryLine(
        `只补护甲：目标 ${bossNumber(item.required_armour_for_full_range)}，` +
        `差 ${bossNumber(item.armour_shortfall)}。`
      ));
    } else if (item.pool_shortfall_for_full_range > 0) {
      card.append(bossSummaryLine('仅补对应抗性或护甲未证明可达；可查看单技能门槛。'));
    }
  }
  if (item.reason) card.append(bossSummaryLine(item.reason));
  if (item.missing_inputs?.length) {
    card.append(bossSummaryLine(`缺失输入：${item.missing_inputs.join('、')}。`));
  }
  const needsArmour = !['Fire', 'Cold', 'Lightning', 'Chaos'].includes(item.damage_type);
  const button = bossNode('button', 'ghost', '查看单技能门槛');
  button.type = 'button';
  if (scenario.armour === null && needsArmour) {
    button.disabled = true;
    card.append(bossSummaryLine('填写护甲后重新审计，才可查看物理技能门槛；未知护甲不会按 0 处理。'));
  }
  button.addEventListener('click', () => bossLoadThreshold(item.skill, item.damage_type));
  card.append(button);
  return card;
}

function bossRenderSkills(data, scenario) {
  const target = bossUi.skills;
  target.replaceChildren();
  const items = Array.isArray(data.skills) ? data.skills : [];
  const assessed = items.filter(item => item.assessable);
  const unknown = items.filter(item => !item.assessable);
  if (!items.length) {
    target.append(bossNode('p', '', '当前没有可展示的技能预设。'));
    return;
  }
  if (assessed.length) {
    target.append(bossNode('h3', '', '已评估项（只按已知承伤排序）'));
    for (const item of assessed) target.append(bossSkillCard(item, scenario));
  }
  if (unknown.length) {
    target.append(bossNode('h3', '', '未评估项（不参与风险排名）'));
    for (const item of unknown) target.append(bossSkillCard(item, scenario));
  }
}

function bossRenderThreshold(data, skill) {
  const target = bossUi.detail;
  target.replaceChildren();
  target.append(bossNode('h3', '', `单技能门槛：${data.skill || skill}`));
  if (!data.found || !data.assessable) {
    target.append(bossSummaryLine(data.reason || '该技能目前不能安全计算门槛。'));
    if (data.missing_inputs?.length) {
      target.append(bossSummaryLine(`缺失输入：${data.missing_inputs.join('、')}。`));
    }
  } else {
    target.append(bossSummaryLine(
      `当前防御池 ${bossNumber(data.target_pool)}；每击承伤 ${bossRange(data.taken_low, data.taken_high)}；` +
      `累计承伤 ${bossRange(data.total_taken_low, data.total_taken_high)}。`
    ));
    target.append(bossSummaryLine(
      `覆盖当前掷骰范围所需最小池子 ${bossNumber(data.minimum_pool_for_full_range)}；` +
      `当前缺口 ${bossNumber(data.pool_shortfall_for_full_range)}。`
    ));
    if (data.required_resist_status === 'reachable') {
      target.append(bossSummaryLine(
        `保持其它输入不变，只补对应抗性时需 ${bossNumber(data.required_resist_for_full_range_pct)}%，` +
        `还差 ${bossNumber(data.resist_shortfall_pct)} 个百分点。`
      ));
    } else if (data.required_resist_status === 'unreachable') {
      target.append(bossSummaryLine('只补对应抗性，在接口上限内不可达。'));
    }
    if (data.required_armour_status === 'reachable') {
      target.append(bossSummaryLine(
        `保持其它输入不变，只补护甲时需 ${bossNumber(data.required_armour_for_full_range)}，` +
        `还差 ${bossNumber(data.armour_shortfall)}。`
      ));
    } else if (data.required_armour_status === 'unreachable') {
      target.append(bossSummaryLine('只补护甲不能证明可达。'));
    }
    if (data.reason) target.append(bossSummaryLine(data.reason));
  }
  if (data.boundary) target.append(bossSummaryLine(`模型边界：${data.boundary}`));
  target.hidden = false;
}

async function bossLoadThreshold(skill, damageType) {
  if (!bossScenario || !skill) return;
  if (bossScenario.armour === null && !['Fire', 'Cold', 'Lightning', 'Chaos'].includes(damageType)) return;
  const epoch = ++bossDetailEpoch;
  const auditEpoch = bossAuditEpoch;
  const scenario = bossScenario;
  bossUi.detail.replaceChildren(bossSummaryLine('正在读取单技能门槛…'));
  bossUi.detail.hidden = false;
  try {
    const { pool, ...base } = scenario;
    if (base.armour === null) delete base.armour;
    const response = await apiFetch('/boss/threshold', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...base, skill, target_pool: pool }),
    });
    if (epoch !== bossDetailEpoch || auditEpoch !== bossAuditEpoch || !bossUi.dialog.open) return;
    if (!response.ok) {
      const message = await bossError(response);
      if (epoch === bossDetailEpoch && auditEpoch === bossAuditEpoch && bossUi.dialog.open) {
        bossUi.detail.replaceChildren(bossSummaryLine(message));
      }
      return;
    }
    const data = await response.json();
    if (epoch !== bossDetailEpoch || auditEpoch !== bossAuditEpoch || !bossUi.dialog.open) return;
    bossRenderThreshold(data, skill);
  } catch (error) {
    if (epoch === bossDetailEpoch && auditEpoch === bossAuditEpoch && bossUi.dialog.open) {
      bossUi.detail.replaceChildren(bossSummaryLine(`门槛读取失败：${String(error.message || error)}`));
    }
  }
}

async function bossRun(event) {
  event?.preventDefault();
  bossClearResults();
  let scenario;
  try {
    scenario = bossReadScenario();
  } catch (error) {
    bossUi.status.textContent = String(error.message || error);
    return;
  }
  const epoch = bossAuditEpoch;
  bossUi.run.disabled = true;
  bossUi.status.textContent = '正在审计当前收录的技能预设…';
  try {
    const response = await apiFetch('/boss/audit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(scenario),
    });
    if (epoch !== bossAuditEpoch || !bossUi.dialog.open) return;
    if (!response.ok) {
      const message = await bossError(response);
      if (epoch === bossAuditEpoch && bossUi.dialog.open) bossUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== bossAuditEpoch || !bossUi.dialog.open) return;
    bossScenario = Object.freeze({ ...scenario });
    bossRenderSummary(data, scenario);
    bossRenderSkills(data, scenario);
    bossUi.status.textContent = '审计完成。未知项仍单独列出，不参与风险排名。';
  } catch (error) {
    if (epoch === bossAuditEpoch && bossUi.dialog.open) {
      bossUi.status.textContent = `审计失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === bossAuditEpoch) bossUi.run.disabled = false;
  }
}

function clearBossWorkbench() {
  bossClearResults();
  bossUi.form.reset();
  bossUi.level.value = '84';
  bossUi.pool.value = '';
  bossUi.hits.value = '1';
  bossUi.roll.value = '';
  bossUi.fire.value = '';
  bossUi.cold.value = '';
  bossUi.lightning.value = '';
  bossUi.chaos.value = '';
  bossUi.armour.value = '';
  bossUi.physical.value = '0';
  bossUi.taken.value = '1';
  bossUi.overwhelm.value = '0';
  bossUi.uber.checked = false;
  if (bossUi.dialog.open) bossUi.dialog.close();
}

window.clearBossWorkbench = clearBossWorkbench;
bossUi.open.addEventListener('click', () => {
  if (!bossUi.dialog.open) bossUi.dialog.showModal();
  bossUi.pool.focus();
});
bossUi.close.addEventListener('click', () => bossUi.dialog.close());
bossUi.dialog.addEventListener('close', () => {
  if (!bossUi.dialog.open) bossClearResults();
});
bossUi.form.addEventListener('submit', bossRun);
bossUi.form.addEventListener('input', bossInputsChanged);
bossUi.form.addEventListener('change', bossInputsChanged);
