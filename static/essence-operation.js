/* 精华状态核对：当前入库精确名称 + POEDB 逐字效果，不推断真实制作路线。 */
'use strict';

const essenceUi = {
  open: document.getElementById('essence-operation-open'),
  dialog: document.getElementById('essence-operation-dialog'),
  close: document.getElementById('essence-operation-close'),
  form: document.getElementById('essence-operation-form'),
  query: document.getElementById('essence-operation-query'),
  name: document.getElementById('essence-operation-name'),
  catalogStatus: document.getElementById('essence-operation-catalog-status'),
  mode: document.getElementById('essence-operation-mode'),
  manual: document.getElementById('essence-operation-manual'),
  itemWrap: document.getElementById('essence-operation-item-wrap'),
  item: document.getElementById('essence-operation-item'),
  rarity: document.getElementById('essence-operation-rarity'),
  level: document.getElementById('essence-operation-level'),
  itemClass: document.getElementById('essence-operation-class'),
  corrupted: document.getElementById('essence-operation-corrupted'),
  mirrored: document.getElementById('essence-operation-mirrored'),
  fractured: document.getElementById('essence-operation-fractured'),
  run: document.getElementById('essence-operation-run'),
  status: document.getElementById('essence-operation-status'),
  result: document.getElementById('essence-operation-result'),
};
const essenceVerdicts = Object.freeze({
  compatible: '已知约束兼容', incompatible: '已知约束不兼容', unknown: '信息不足',
});
const essenceChecks = Object.freeze({ pass: '通过', fail: '不通过', unknown: '未知' });
const essenceModes = Object.freeze({
  upgrade_normal_to_rare: '普通物品升级为稀有物品',
  reroll_rare_properties: '重置稀有物品属性',
  unknown: '未知（不执行状态转移）',
});
let essenceEpoch = 0;
let essenceCatalogEpoch = 0;
let essenceCatalog = [];
let essenceCatalogLoaded = false;

function essenceNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function essenceSection(title) {
  const section = essenceNode('section', 'essence-operation-section');
  section.append(essenceNode('h3', '', title));
  return section;
}

function essenceText(value) {
  return value === null || value === undefined || value === '' ? '未知' : String(value);
}

function essenceSafeUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol === 'https:' && url.hostname === 'poedb.tw' &&
        !url.username && !url.password && !url.port) return url.href;
  } catch (_) { /* 不可信来源只显示文字。 */ }
  return '';
}

function essenceLink(raw, label) {
  const safe = essenceSafeUrl(raw);
  if (!safe) return null;
  const link = essenceNode('a', '', label);
  link.href = safe;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function essenceUpdateRun() {
  essenceUi.run.disabled = !essenceCatalogLoaded ||
    !essenceCatalog.some(item => item.name_zh === essenceUi.name.value);
}

function essenceInvalidate() {
  essenceEpoch += 1;
  essenceUi.status.textContent = '';
  essenceUi.result.replaceChildren();
  essenceUi.result.hidden = true;
  essenceUpdateRun();
}

function essenceInputChanged() {
  essenceInvalidate();
  essenceUi.status.textContent = '输入已变化；请重新核对。';
}

function essenceRenderCatalog() {
  const query = essenceUi.query.value.trim().toLocaleLowerCase();
  const tier = /^t\d+$/i.test(query) ? Number(query.slice(1)) : null;
  const matches = essenceCatalog.filter(item => !query ||
    item.name_zh.toLocaleLowerCase().includes(query) ||
    String(item.name_en || '').toLocaleLowerCase().includes(query) ||
    (tier !== null && item.tier === tier));
  essenceUi.name.replaceChildren();
  const placeholder = essenceNode('option', '', '请选择精华');
  placeholder.value = '';
  essenceUi.name.append(placeholder);
  for (const item of matches) {
    const label = `T${item.tier} · ${item.name_zh}` +
      (item.name_en ? ` · ${item.name_en}` : '');
    const option = essenceNode('option', '', label);
    option.value = item.name_zh;
    essenceUi.name.append(option);
  }
  essenceUi.name.value = '';
  essenceUi.catalogStatus.textContent = essenceCatalogLoaded
    ? `当前入库 ${essenceCatalog.length} 件；筛选后 ${matches.length} 件。请选择精确名称。`
    : '正在读取当前入库精华清单…';
  essenceUpdateRun();
}

async function essenceError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string') return payload.detail;
    if (typeof payload?.detail?.reason === 'string') return payload.detail.reason;
  } catch (_) { /* 非 JSON 错误使用状态码。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

async function essenceLoadCatalog() {
  const epoch = ++essenceCatalogEpoch;
  essenceCatalogLoaded = false;
  essenceCatalog = [];
  essenceRenderCatalog();
  try {
    const response = await apiFetch('/craft/essences');
    if (epoch !== essenceCatalogEpoch || !essenceUi.dialog.open) return;
    if (!response.ok) {
      const message = await essenceError(response);
      if (epoch === essenceCatalogEpoch && essenceUi.dialog.open) {
        essenceUi.catalogStatus.textContent = message;
      }
      return;
    }
    const data = await response.json();
    if (epoch !== essenceCatalogEpoch || !essenceUi.dialog.open) return;
    if (!Array.isArray(data.items) || data.total !== data.items.length) {
      essenceUi.catalogStatus.textContent = '精华清单结构不完整，不能选择操作。';
      return;
    }
    essenceCatalog = data.items.filter(item =>
      typeof item.name_zh === 'string' && item.name_zh &&
      Number.isInteger(item.tier) && item.tier >= 0);
    if (essenceCatalog.length !== data.items.length || !essenceCatalog.length) {
      essenceCatalog = [];
      essenceUi.catalogStatus.textContent = '当前没有可核对的精华清单。';
      return;
    }
    essenceCatalogLoaded = true;
    essenceRenderCatalog();
  } catch (error) {
    if (epoch === essenceCatalogEpoch && essenceUi.dialog.open) {
      essenceUi.catalogStatus.textContent = `清单加载失败：${String(error.message || error)}`;
    }
  }
}

function essenceModeChanged() {
  const itemMode = essenceUi.mode.value === 'item';
  essenceUi.manual.hidden = itemMode;
  essenceUi.itemWrap.hidden = !itemMode;
  essenceInputChanged();
}

function essenceReadRequest() {
  const essence = essenceUi.name.value;
  if (!essenceCatalogLoaded || !essenceCatalog.some(item => item.name_zh === essence)) {
    throw new Error('请从当前入库清单选择精确精华名称。');
  }
  if (essenceUi.mode.value === 'item') {
    const item_text = essenceUi.item.value.trim();
    if (!item_text || item_text.length > 20_000) {
      throw new Error('完整装备复制文本必须是 1 到 20000 字。');
    }
    return { essence, item_text };
  }
  if (essenceUi.mode.value !== 'manual') throw new Error('装备状态来源无效。');
  const rarity = essenceUi.rarity.value;
  if (rarity && !['普通', '魔法', '稀有', '传奇'].includes(rarity)) {
    throw new Error('稀有度无效。');
  }
  const levelText = essenceUi.level.value.trim();
  let item_level = null;
  if (levelText) {
    if (!/^\d+$/.test(levelText)) throw new Error('物品等级必须是 1 到 100 的整数。');
    item_level = Number(levelText);
    if (!Number.isInteger(item_level) || item_level < 1 || item_level > 100) {
      throw new Error('物品等级必须是 1 到 100 的整数。');
    }
  }
  const item_class = essenceUi.itemClass.value.trim();
  if (item_class.length > 80) throw new Error('装备类别最多 80 字。');
  return {
    essence, rarity: rarity || null, item_level, item_class: item_class || null,
    corrupted: essenceUi.corrupted.checked, mirrored: essenceUi.mirrored.checked,
    fractured: essenceUi.fractured.checked,
  };
}

function essenceRender(data) {
  essenceUi.result.replaceChildren();
  const verdict = essenceSection('结论');
  verdict.append(essenceNode('h4', '', essenceVerdicts[data.verdict] || '信息不足'));
  verdict.append(essenceNode('p', '',
    '兼容只表示已提供状态通过来源效果、物等上限与装备类别检查，不表示完整游戏可执行。'));
  essenceUi.result.append(verdict);

  const source = essenceSection(`${essenceText(data.essence)} · 来源效果`);
  source.append(essenceNode('p', '', `T${essenceText(data.tier)}；` +
    `物品等级上限：${data.level_cap === null ? '页面未设置' : essenceText(data.level_cap)}。`));
  source.append(essenceNode('p', '', data.base_effect || '来源没有提供效果原文。'));
  source.append(essenceNode('p', '',
    `来源 ${data.source || '未知'}；抓取 ${data.fetched_at || '未知'}；` +
    `固定来源 ID ${data.metadata_id || '未知'}。`));
  const sourceLink = essenceLink(data.source_url, '打开 POEDB 来源页');
  if (sourceLink) source.append(sourceLink);
  essenceUi.result.append(source);

  const before = data.before || {};
  const after = data.after || {};
  const state = essenceSection('输入状态与条件后状态');
  state.append(essenceNode('p', '',
    `输入：${essenceText(before.rarity)}；物品等级 ${essenceText(before.item_level)}；` +
    `装备类别 ${essenceText(before.item_class)}。`));
  state.append(essenceNode('p', '',
    `特殊状态：腐化 ${before.corrupted ? '是' : '否'}；镜像 ${before.mirrored ? '是' : '否'}；` +
    `裂隙 ${before.fractured ? '是' : '否'}。`));
  if (before.incomplete_reason) {
    state.append(essenceNode('p', 'essence-operation-warning',
      `装备文本未完整识别：${before.incomplete_reason}`));
  }
  state.append(essenceNode('h4', '', '条件后状态（仅当操作实际成功执行时）'));
  if (data.verdict === 'incompatible') {
    state.append(essenceNode('p', 'essence-operation-warning',
      '当前已知约束不兼容；以下字段不是可执行结果。'));
  }
  state.append(essenceNode('p', '',
    `模式：${essenceModes[after.operation_mode] || '未知'}；` +
    `稀有度：${after.operation_mode === 'unknown' ? '未知' : essenceText(after.rarity)}；` +
    '前后缀数量：未知。'));
  // 来源效果漂移时后端会清空 after.guaranteed_modifier；不得把原始类别表冒充结果。
  const guaranteed = after.guaranteed_modifier;
  state.append(essenceNode('p', '', guaranteed
    ? `${essenceText(guaranteed.item_class)}保底词缀：${essenceText(guaranteed.text)}`
    : '当前装备类别的保底词缀：未知。'));
  essenceUi.result.append(state);

  const checks = essenceSection('已知约束检查');
  const rows = Array.isArray(data.checks) ? data.checks : [];
  if (!rows.length) checks.append(essenceNode('p', '', '没有可展示的结构化检查。'));
  for (const check of rows) {
    const row = essenceNode('div', 'essence-operation-check');
    row.append(essenceNode('strong', '',
      `${check.key || '检查'}：${essenceChecks[check.status] || '未知'}`));
    row.append(essenceNode('p', '', check.summary || '未提供说明'));
    const link = essenceLink(check.source_url, '检查来源');
    if (link) row.append(link);
    checks.append(row);
  }
  if (Array.isArray(data.unknown_fields) && data.unknown_fields.length) {
    checks.append(essenceNode('h4', '', '仍未知'));
    const list = essenceNode('ul');
    for (const item of data.unknown_fields) list.append(essenceNode('li', '', item));
    checks.append(list);
  }
  essenceUi.result.append(checks);
  const boundary = essenceSection('适用边界');
  boundary.append(essenceNode('p', '', data.boundary ||
    '不生成额外词缀、最终数值、成功率、步骤、成本或路线。'));
  essenceUi.result.append(boundary);
  essenceUi.result.hidden = false;
}

async function essenceSubmit(event) {
  event?.preventDefault();
  essenceInvalidate();
  let request;
  try {
    request = essenceReadRequest();
  } catch (error) {
    essenceUi.status.textContent = String(error.message || error);
    return;
  }
  const epoch = essenceEpoch;
  essenceUi.run.disabled = true;
  essenceUi.status.textContent = '正在核对精华效果与装备状态…';
  try {
    const response = await apiFetch('/craft/essence', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (epoch !== essenceEpoch || !essenceUi.dialog.open) return;
    if (!response.ok) {
      const message = await essenceError(response);
      if (epoch === essenceEpoch && essenceUi.dialog.open) essenceUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== essenceEpoch || !essenceUi.dialog.open) return;
    if (!data.found) {
      essenceUi.status.textContent = '当前来源未收录该精华；不能按相似名称猜操作。';
      return;
    }
    essenceRender(data);
    essenceUi.status.textContent = '核对完成；条件后状态不等于实际制作结果。';
  } catch (error) {
    if (epoch === essenceEpoch && essenceUi.dialog.open) {
      essenceUi.status.textContent = `核对失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === essenceEpoch) essenceUpdateRun();
  }
}

function openEssenceOperation(itemText = '') {
  essenceInvalidate();
  if (itemText) {
    essenceUi.mode.value = 'item';
    essenceUi.item.value = String(itemText);
  }
  essenceUi.manual.hidden = essenceUi.mode.value === 'item';
  essenceUi.itemWrap.hidden = essenceUi.mode.value !== 'item';
  if (!essenceUi.dialog.open) essenceUi.dialog.showModal();
  essenceLoadCatalog();
  essenceUi.query.focus();
}

function clearEssenceOperation() {
  essenceCatalogEpoch += 1;
  essenceCatalog = [];
  essenceCatalogLoaded = false;
  essenceInvalidate();
  essenceUi.form.reset();
  essenceUi.query.value = '';
  essenceUi.name.replaceChildren();
  essenceUi.name.value = '';
  essenceUi.catalogStatus.textContent = '';
  essenceUi.mode.value = 'manual';
  essenceUi.manual.hidden = false;
  essenceUi.itemWrap.hidden = true;
  essenceUi.item.value = '';
  essenceUi.rarity.value = '';
  essenceUi.level.value = '';
  essenceUi.itemClass.value = '';
  essenceUi.corrupted.checked = false;
  essenceUi.mirrored.checked = false;
  essenceUi.fractured.checked = false;
  if (essenceUi.dialog.open) essenceUi.dialog.close();
}

window.openEssenceOperation = openEssenceOperation;
window.clearEssenceOperation = clearEssenceOperation;
essenceUi.open.addEventListener('click', () => openEssenceOperation());
essenceUi.close.addEventListener('click', () => essenceUi.dialog.close());
essenceUi.dialog.addEventListener('close', () => {
  essenceCatalogEpoch += 1;
  essenceCatalogLoaded = false;
  essenceCatalog = [];
  essenceUi.name.replaceChildren();
  essenceUi.catalogStatus.textContent = '';
  essenceInvalidate();
});
essenceUi.query.addEventListener('input', essenceRenderCatalog);
essenceUi.mode.addEventListener('change', essenceModeChanged);
essenceUi.form.addEventListener('submit', essenceSubmit);
essenceUi.form.addEventListener('input', essenceInputChanged);
essenceUi.form.addEventListener('change', essenceInputChanged);
