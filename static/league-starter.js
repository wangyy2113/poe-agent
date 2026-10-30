/* 用户 PoB 快照阶段路线；所有外部文本经 textContent 渲染。 */
'use strict';

const leagueUi = {
  open: document.getElementById('league-open'),
  dialog: document.getElementById('league-dialog'),
  close: document.getElementById('league-close'),
  form: document.getElementById('league-form'),
  current: document.getElementById('league-current'),
  stages: document.getElementById('league-stages'),
  add: document.getElementById('league-add'),
  metrics: document.getElementById('league-metrics'),
  run: document.getElementById('league-run'),
  status: document.getElementById('league-status'),
  result: document.getElementById('league-result'),
};
let leagueRows = [];
let leagueEpoch = 0;

function leagueNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function leagueField(title, control) {
  const label = leagueNode('label', '', title);
  label.append(control);
  return label;
}

function leagueInvalidate() {
  leagueEpoch += 1;
  leagueUi.run.disabled = false;
  leagueUi.status.textContent = '';
  leagueUi.result.hidden = true;
  leagueUi.result.replaceChildren();
}

function leagueRenderRows() {
  leagueUi.stages.replaceChildren(...leagueRows.map(item => item.row));
  leagueUi.add.disabled = leagueRows.length >= 5;
  for (const item of leagueRows) item.remove.disabled = leagueRows.length <= 1;
}

function leagueAddRow() {
  if (leagueRows.length >= 5) return;
  const row = leagueNode('fieldset', 'league-stage-input');
  const legend = leagueNode('legend', '', `阶段 ${leagueRows.length + 1}`);
  const label = leagueNode('input');
  label.required = true;
  label.maxLength = 80;
  label.placeholder = '例如：进入白图';
  const goal = leagueNode('input');
  goal.required = true;
  goal.maxLength = 200;
  goal.placeholder = '你希望在此阶段完成的目标';
  const code = leagueNode('textarea');
  code.required = true;
  code.maxLength = 200000;
  code.rows = 3;
  code.spellcheck = false;
  code.placeholder = '此阶段的完整 PoB 分享码或白名单链接';
  const remove = leagueNode('button', 'ghost', '移除阶段');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    leagueRows = leagueRows.filter(item => item.row !== row);
    leagueRows.forEach((item, index) => { item.legend.textContent = `阶段 ${index + 1}`; });
    leagueRenderRows();
    leagueInvalidate();
  });
  row.append(legend, leagueField('阶段名称', label), leagueField('阶段目标', goal),
    leagueField('阶段 PoB', code), remove);
  leagueRows.push({ row, legend, label, goal, code, remove });
  leagueRenderRows();
  leagueInvalidate();
}

function leagueList(section, title, values) {
  if (!Array.isArray(values) || !values.length) return;
  section.append(leagueNode('h4', '', title));
  const list = leagueNode('ul');
  for (const value of values) list.append(leagueNode('li', '', value));
  section.append(list);
}

function leagueValue(value) {
  return value === null || value === undefined || value === '' ? '未知' : String(value);
}

function leagueRenderMetricTable(stage) {
  const wrap = leagueNode('div', 'league-table-scroll');
  const table = leagueNode('table', 'league-metric-table');
  const head = leagueNode('thead');
  const header = leagueNode('tr');
  for (const text of ['指标', '上阶段', '本阶段', '变化', '状态与原因']) {
    header.append(leagueNode('th', '', text));
  }
  head.append(header);
  const body = leagueNode('tbody');
  for (const metric of stage.metrics || []) {
    const row = leagueNode('tr');
    const delta = metric.status === 'ready'
      ? `${leagueValue(metric.delta)}${metric.unit || ''}` : '未知';
    const cells = [
      metric.label,
      `${leagueValue(metric.previous_value)}${metric.previous_field ? ` (${metric.previous_field})` : ''}`,
      `${leagueValue(metric.target_value)}${metric.target_field ? ` (${metric.target_field})` : ''}`,
      delta,
      metric.status === 'ready' ? '可比' : `未知：${metric.reason || '口径不足'}`,
    ];
    for (const text of cells) row.append(leagueNode('td', '', text));
    body.append(row);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function leagueRender(data) {
  leagueUi.result.replaceChildren();
  leagueUi.result.append(leagueNode('p', 'league-summary',
    `${data.stage_count} 个阶段；数值差额可比 ${data.numeric_ready}/${data.metric_transition_count} 项。`));
  for (const stage of data.stages || []) {
    const section = leagueNode('section', 'league-stage-result');
    section.append(leagueNode('h3', '', `${stage.sequence}. ${stage.label}`));
    section.append(leagueNode('p', '', `你的目标：${stage.goal}`));
    section.append(leagueNode('p', '',
      `目标快照：等级 ${leagueValue(stage.target_level)}；${leagueValue(stage.target_class)} / ` +
      `${leagueValue(stage.target_ascendancy)}；当前计算技能 ${leagueValue(stage.target_skill)}；` +
      `树版本 ${leagueValue(stage.target_tree_version)}。`));
    section.append(leagueNode('p', '', `从「${stage.previous_label}」到本阶段的结构差异：`));
    const structure = stage.structure || {};
    leagueList(section, '变动装备槽', structure.changed_item_slots);
    if (Array.isArray(stage.equipment_targets) && stage.equipment_targets.length) {
      section.append(leagueNode('h4', '', '目标装备'));
      const items = leagueNode('ul');
      for (const item of stage.equipment_targets) {
        const target = item.status === 'present'
          ? `${item.name || '未提供名称'} / ${item.base_type || '未知底材'}`
          : item.status === 'empty' ? '目标快照为空' : '未知';
        items.append(leagueNode('li', '', `${item.slot}：${target}。${item.note || ''}`));
      }
      section.append(items);
    }
    leagueList(section, '新增或升级宝石', structure.added_gems);
    leagueList(section, '移除或替换宝石', structure.removed_gems);
    leagueList(section, '新增天赋节点 ID', structure.passive_nodes_added);
    leagueList(section, '移除天赋节点 ID', structure.passive_nodes_removed);
    if (structure.note) section.append(leagueNode('p', 'league-note', structure.note));
    if (stage.context_status !== 'ready') {
      leagueList(section, '数值不可比原因', stage.context_reasons);
    }
    section.append(leagueRenderMetricTable(stage));
    section.append(leagueNode('p', 'league-note', stage.verification));
    leagueUi.result.append(section);
  }
  leagueUi.result.append(leagueNode('p', 'league-note', data.note || ''));
  leagueUi.result.hidden = false;
}

async function leagueSubmit(event) {
  event.preventDefault();
  leagueInvalidate();
  const epoch = leagueEpoch;
  const currentCode = leagueUi.current.value.trim();
  const stages = leagueRows.map(item => ({
    label: item.label.value.trim(), goal: item.goal.value.trim(), code: item.code.value.trim(),
  }));
  if (!currentCode || stages.some(item => !item.label || !item.goal || !item.code)) {
    leagueUi.status.textContent = '请填写当前 PoB 和每个阶段的名称、目标与 PoB。';
    return;
  }
  const metricText = leagueUi.metrics.value.trim();
  const metrics = metricText ? metricText.split(',').map(value => value.trim()).filter(Boolean) : null;
  const body = { current_code: currentCode, stages, metrics };
  leagueUi.run.disabled = true;
  leagueUi.status.textContent = '正在核对阶段快照…';
  try {
    const response = await apiFetch('/planner/league-starter', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await response.json();
    if (epoch !== leagueEpoch) return;
    if (!response.ok) {
      const detail = data.detail || {};
      const reason = typeof detail === 'string' ? detail : detail.reason || `HTTP ${response.status}`;
      leagueUi.status.textContent = `${detail.stage ? `${detail.stage}：` : ''}${reason}`;
      return;
    }
    leagueRender(data);
    leagueUi.status.textContent = '阶段路线已生成；未知项保留在表中。';
  } catch (error) {
    if (epoch === leagueEpoch) leagueUi.status.textContent = `请求失败：${String(error.message || error)}`;
  } finally {
    if (epoch === leagueEpoch) leagueUi.run.disabled = false;
  }
}

function clearLeagueStarter() {
  leagueInvalidate();
  leagueUi.current.value = '';
  leagueUi.metrics.value = '';
  leagueRows = [];
  leagueRenderRows();
  leagueAddRow();
  if (leagueUi.dialog.open) leagueUi.dialog.close();
}
window.clearLeagueStarter = clearLeagueStarter;

leagueUi.open.addEventListener('click', () => leagueUi.dialog.showModal());
leagueUi.close.addEventListener('click', () => leagueUi.dialog.close());
leagueUi.dialog.addEventListener('close', leagueInvalidate);
leagueUi.add.addEventListener('click', leagueAddRow);
leagueUi.form.addEventListener('input', leagueInvalidate);
leagueUi.form.addEventListener('submit', leagueSubmit);
leagueAddRow();
