/* 基础通货状态核对：只呈现 POEDB 效果与结构化名额，不补制作路线。 */
'use strict';

const currencyUi = {
  open: document.getElementById('currency-operation-open'),
  dialog: document.getElementById('currency-operation-dialog'),
  close: document.getElementById('currency-operation-close'),
  form: document.getElementById('currency-operation-form'),
  name: document.getElementById('currency-operation-name'),
  mode: document.getElementById('currency-operation-mode'),
  manual: document.getElementById('currency-operation-manual'),
  itemWrap: document.getElementById('currency-operation-item-wrap'),
  item: document.getElementById('currency-operation-item'),
  rarity: document.getElementById('currency-operation-rarity'),
  kind: document.getElementById('currency-operation-kind'),
  prefix: document.getElementById('currency-operation-prefix'),
  suffix: document.getElementById('currency-operation-suffix'),
  corrupted: document.getElementById('currency-operation-corrupted'),
  mirrored: document.getElementById('currency-operation-mirrored'),
  fractured: document.getElementById('currency-operation-fractured'),
  guard: document.getElementById('currency-operation-guard'),
  run: document.getElementById('currency-operation-run'),
  status: document.getElementById('currency-operation-status'),
  result: document.getElementById('currency-operation-result'),
};

const currencyNames = new Set([
  '增幅石', '崇高石', '重铸石', '神圣石', '剥离石',
  '改造石', '混沌石', '富豪石', '机会石', '点金石',
]);
const currencyKinds = new Set(['default', 'jewel', 'flask', 'graft', 'jewel_abyss_corrupted']);
const currencyGuards = new Set([
  '前缀无法被变更', '后缀无法被变更', '无法骰出攻击词缀', '无法骰出法术词缀',
]);
const currencyVerdicts = Object.freeze({
  compatible: '已知约束兼容', incompatible: '已知约束不兼容', unknown: '信息不足',
});
const currencyCheckLabels = Object.freeze({ pass: '通过', fail: '不通过', unknown: '未知' });
const currencyIdentityLabels = Object.freeze({
  unchanged: '现有词缀身份不变',
  one_random_modifier_added: '新增一条随机词缀，具体词缀未知',
  all_properties_rerolled: '现有属性被新的随机属性重铸',
  all_properties_removed: '属性全部移除',
  one_random_property_removed: '随机移除一条属性',
  not_specified_by_source: '来源原文未说明',
});
const currencyValueLabels = Object.freeze({
  unchanged: '现有词缀数值不变',
  rerolled_with_properties: '随属性一并重铸',
  random_modifier_values_rerolled: '随机词缀数值被重置',
});
let currencyEpoch = 0;

function currencyNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function currencySection(title) {
  const section = currencyNode('section', 'currency-operation-section');
  section.append(currencyNode('h3', '', title));
  return section;
}

function currencyText(value) {
  return value === null || value === undefined || value === '' ? '未知' : String(value);
}

function currencySafeUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return '';
    if (['poedb.tw', 'www.craftofexile.com', 'craftofexile.com'].includes(url.hostname)) {
      return url.href;
    }
  } catch (_) { /* 非可信来源只显示文字。 */ }
  return '';
}

function currencyLink(raw, label) {
  const safe = currencySafeUrl(raw);
  if (!safe) return null;
  const link = currencyNode('a', '', label);
  link.href = safe;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function currencyList(section, heading, values) {
  if (!Array.isArray(values) || !values.length) return;
  section.append(currencyNode('h4', '', heading));
  const list = currencyNode('ul');
  for (const item of values) list.append(currencyNode('li', '', item));
  section.append(list);
}

function currencyInvalidate() {
  currencyEpoch += 1;
  currencyUi.run.disabled = false;
  currencyUi.status.textContent = '';
  currencyUi.result.replaceChildren();
  currencyUi.result.hidden = true;
}

function currencyInputChanged() {
  currencyInvalidate();
  currencyUi.status.textContent = '输入已变化；请重新核对。';
}

function currencyModeChanged() {
  const itemMode = currencyUi.mode.value === 'item';
  currencyUi.manual.hidden = itemMode;
  currencyUi.itemWrap.hidden = !itemMode;
  currencyInputChanged();
}

function currencyOptionalCount(node, label) {
  const raw = node.value.trim();
  if (!raw) return null;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0 || value > 6) {
    throw new Error(`${label}必须是 0 到 6 的整数，或留空表示未知。`);
  }
  return value;
}

function currencyReadRequest() {
  const operation = currencyUi.name.value;
  if (!currencyNames.has(operation)) throw new Error('请选择已审计的基础通货操作。');
  const guard = currencyUi.guard.value;
  if (guard && !currencyGuards.has(guard)) throw new Error('Metamod guard 必须是来源矩阵逐字列名。');
  const common = { operation, metamod_guard: guard || null };
  if (currencyUi.mode.value === 'item') {
    const item_text = currencyUi.item.value.trim();
    if (!item_text || item_text.length > 20_000) {
      throw new Error('完整装备复制文本必须是 1 到 20000 字。');
    }
    return { ...common, item_text };
  }
  if (currencyUi.mode.value !== 'manual') throw new Error('装备状态来源无效。');
  const rarity = currencyUi.rarity.value;
  if (rarity && !['普通', '魔法', '稀有', '传奇'].includes(rarity)) {
    throw new Error('稀有度无效。');
  }
  const item_kind = currencyUi.kind.value;
  if (!currencyKinds.has(item_kind)) throw new Error('装备种类无效。');
  return {
    ...common, rarity: rarity || null,
    prefix_count: currencyOptionalCount(currencyUi.prefix, '前缀数量'),
    suffix_count: currencyOptionalCount(currencyUi.suffix, '后缀数量'),
    item_kind,
    corrupted: currencyUi.corrupted.checked,
    mirrored: currencyUi.mirrored.checked,
    fractured: currencyUi.fractured.checked,
  };
}

async function currencyError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string') return payload.detail;
    if (typeof payload?.detail?.reason === 'string') return payload.detail.reason;
  } catch (_) { /* 非 JSON 错误使用状态码。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

function currencyRenderSource(data) {
  const section = currencySection(`${currencyText(data.operation)} · 来源效果`);
  section.append(currencyNode('p', '', data.effect_text || '来源没有提供效果原文。'));
  section.append(currencyNode('p', '',
    `来源 ${data.source || '未知'}；抓取 ${data.fetched_at || '未知'}；` +
    `固定来源 ID ${data.metadata_id || '未知'}。`));
  const link = currencyLink(data.source_url, '打开 POEDB 来源页');
  if (link) section.append(link);
  currencyUi.result.append(section);
}

function currencyRenderState(data) {
  const section = currencySection('输入状态与条件后状态');
  const before = data.before || {};
  section.append(currencyNode('p', '',
    `输入：${currencyText(before.rarity)}；前缀 ${currencyText(before.prefix_count)}；` +
    `后缀 ${currencyText(before.suffix_count)}；装备种类 ${currencyText(before.item_kind)}。`));
  section.append(currencyNode('p', '',
    `特殊状态：腐化 ${before.corrupted ? '是' : '否'}；镜像 ${before.mirrored ? '是' : '否'}；` +
    `裂隙 ${before.fractured ? '是' : '否'}。`));
  if (before.incomplete_reason) {
    section.append(currencyNode('p', 'currency-operation-warning',
      `装备文本未完整识别：${before.incomplete_reason}`));
  }
  section.append(currencyNode('h4', '', '条件后状态（仅当操作实际成功执行时）'));
  const after = data.after || {};
  if (Array.isArray(after.possible_rarities) && after.possible_rarities.length) {
    section.append(currencyNode('p', '', `可能稀有度：${after.possible_rarities.join(' / ')}。`));
  } else {
    section.append(currencyNode('p', '', `稀有度：${currencyText(after.rarity)}。`));
  }
  if (Array.isArray(after.possible_affix_counts) && after.possible_affix_counts.length) {
    const choices = after.possible_affix_counts.map(item =>
      `${currencyText(item.prefix_count)} 前缀 + ${currencyText(item.suffix_count)} 后缀`
    );
    section.append(currencyNode('p', '', `词缀计数可能为：${choices.join(' 或 ')}。`));
  } else {
    section.append(currencyNode('p', '',
      `词缀计数：${currencyText(after.prefix_count)} 前缀 + ${currencyText(after.suffix_count)} 后缀。`));
  }
  section.append(currencyNode('p', '',
    `词缀身份：${currencyIdentityLabels[after.modifier_identities] || currencyText(after.modifier_identities)}；` +
    `数值：${currencyValueLabels[after.modifier_values] || currencyText(after.modifier_values)}。`));
  currencyUi.result.append(section);
}

function currencyRenderChecks(data) {
  const section = currencySection('已知约束检查');
  const checks = Array.isArray(data.checks) ? data.checks : [];
  if (!checks.length) section.append(currencyNode('p', '', '没有可展示的结构化检查。'));
  for (const check of checks) {
    const row = currencyNode('div', 'currency-operation-check');
    row.append(currencyNode('strong', '',
      `${check.key || '检查'}：${currencyCheckLabels[check.status] || '未知'}`));
    row.append(currencyNode('p', '', check.summary || '未提供说明'));
    const link = currencyLink(check.source_url, '检查来源');
    if (link) row.append(link);
    section.append(row);
  }
  currencyList(section, '仍未知', data.unknown_fields);
  currencyUi.result.append(section);
}

function currencyRenderMetamod(data) {
  const matrix = data.metamod_interaction || {};
  if (matrix.status === 'not_requested' && !data.metamod_guard) return;
  const section = currencySection('Metamod 矩阵：独立来源原值');
  section.append(currencyNode('p', '',
    `${matrix.operation || data.operation || '未知操作'} × ` +
    `${matrix.guard || data.metamod_guard || '未知 guard'}。`));
  if (matrix.status === 'recorded') {
    section.append(currencyNode('p', '',
      `矩阵原值：${currencyText(matrix.raw_value)}；原始注释：${matrix.note_text || '无'}；` +
      `版本范围：${matrix.version_scope || '未知'}。`));
    const link = currencyLink(matrix.source_url, '打开 Craft of Exile 矩阵');
    if (link) section.append(link);
    section.append(currencyNode('p', '',
      'YES / NO / N/A 只按来源原文披露；不解释外延，也不改写基础通货状态转移。'));
  } else {
    section.append(currencyNode('p', 'currency-operation-warning',
      `矩阵没有可用的逐字记录：${matrix.reason || '来源空白或未记录'}。`));
    section.append(currencyNode('p', '',
      '基础通货结论单独保留；含该 guard 的整体结论不能冒充已知兼容。'));
  }
  currencyUi.result.append(section);
}

function currencyRender(data) {
  currencyUi.result.replaceChildren();
  const verdict = currencySection('结论');
  verdict.append(currencyNode('h4', '', currencyVerdicts[data.verdict] || '未知'));
  if (data.metamod_guard) {
    verdict.append(currencyNode('p', '',
      `基础通货单独结论：${currencyVerdicts[data.operation_verdict] || '未知'}；` +
      `含元词缀矩阵的整体结论：${currencyVerdicts[data.verdict] || '未知'}。`));
  }
  verdict.append(currencyNode('p', '',
    '兼容只表示已提供状态通过来源效果与名额检查，不表示完整游戏可执行性。'));
  currencyUi.result.append(verdict);
  currencyRenderSource(data);
  currencyRenderState(data);
  currencyRenderChecks(data);
  currencyRenderMetamod(data);
  const boundary = currencySection('适用边界');
  boundary.append(currencyNode('p', '', data.boundary ||
    '不生成具体词缀、成功率、步骤、成本或制作路线。'));
  currencyUi.result.append(boundary);
  currencyUi.result.hidden = false;
}

async function currencySubmit(event) {
  event?.preventDefault();
  currencyInvalidate();
  let request;
  try {
    request = currencyReadRequest();
  } catch (error) {
    currencyUi.status.textContent = String(error.message || error);
    return;
  }
  const epoch = currencyEpoch;
  currencyUi.run.disabled = true;
  currencyUi.status.textContent = '正在核对来源效果与装备状态…';
  try {
    const response = await apiFetch('/craft/operation', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (epoch !== currencyEpoch || !currencyUi.dialog.open) return;
    if (!response.ok) {
      const message = await currencyError(response);
      if (epoch === currencyEpoch && currencyUi.dialog.open) currencyUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== currencyEpoch || !currencyUi.dialog.open) return;
    if (!data.found) {
      currencyUi.status.textContent = '来源未收录该操作；不能按相似通货猜测语义。';
      return;
    }
    currencyRender(data);
    currencyUi.status.textContent = '核对完成；条件后状态不等于实际制作结果。';
  } catch (error) {
    if (epoch === currencyEpoch && currencyUi.dialog.open) {
      currencyUi.status.textContent = `核对失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === currencyEpoch) currencyUi.run.disabled = false;
  }
}

function openCurrencyOperation(itemText = '') {
  currencyInvalidate();
  if (itemText) {
    currencyUi.mode.value = 'item';
    currencyUi.item.value = String(itemText);
  }
  currencyUi.manual.hidden = currencyUi.mode.value === 'item';
  currencyUi.itemWrap.hidden = currencyUi.mode.value !== 'item';
  if (!currencyUi.dialog.open) currencyUi.dialog.showModal();
  currencyUi.name.focus();
}

function clearCurrencyOperation() {
  currencyInvalidate();
  currencyUi.form.reset();
  currencyUi.name.value = '';
  currencyUi.mode.value = 'manual';
  currencyUi.manual.hidden = false;
  currencyUi.itemWrap.hidden = true;
  currencyUi.item.value = '';
  currencyUi.rarity.value = '';
  currencyUi.kind.value = 'default';
  currencyUi.prefix.value = '';
  currencyUi.suffix.value = '';
  currencyUi.corrupted.checked = false;
  currencyUi.mirrored.checked = false;
  currencyUi.fractured.checked = false;
  currencyUi.guard.value = '';
  if (currencyUi.dialog.open) currencyUi.dialog.close();
}

window.openCurrencyOperation = openCurrencyOperation;
window.clearCurrencyOperation = clearCurrencyOperation;
currencyUi.open.addEventListener('click', () => openCurrencyOperation());
currencyUi.close.addEventListener('click', () => currencyUi.dialog.close());
currencyUi.dialog.addEventListener('close', () => {
  if (!currencyUi.dialog.open) currencyInvalidate();
});
currencyUi.mode.addEventListener('change', currencyModeChanged);
currencyUi.form.addEventListener('submit', currencySubmit);
currencyUi.form.addEventListener('input', currencyInputChanged);
currencyUi.form.addEventListener('change', currencyInputChanged);
