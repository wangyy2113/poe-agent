/* 古灵通货核对：12 种逐字效果、三态输入和独立 Metamod 原值。 */
'use strict';

const eldritchUi = {
  open: document.getElementById('eldritch-operation-open'),
  dialog: document.getElementById('eldritch-operation-dialog'),
  close: document.getElementById('eldritch-operation-close'),
  form: document.getElementById('eldritch-operation-form'),
  name: document.getElementById('eldritch-operation-name'),
  catalogStatus: document.getElementById('eldritch-operation-catalog-status'),
  mode: document.getElementById('eldritch-operation-mode'),
  actionNote: document.getElementById('eldritch-operation-action-note'),
  manual: document.getElementById('eldritch-operation-manual'),
  itemWrap: document.getElementById('eldritch-operation-item-wrap'),
  item: document.getElementById('eldritch-operation-item'),
  slotWrap: document.getElementById('eldritch-operation-slot-wrap'),
  dominantWrap: document.getElementById('eldritch-operation-dominant-wrap'),
  implicitWrap: document.getElementById('eldritch-operation-implicit-wrap'),
  slot: document.getElementById('eldritch-operation-slot'),
  rarity: document.getElementById('eldritch-operation-rarity'),
  dominance: document.getElementById('eldritch-operation-dominance'),
  prefix: document.getElementById('eldritch-operation-prefix'),
  suffix: document.getElementById('eldritch-operation-suffix'),
  searing: document.getElementById('eldritch-operation-searing'),
  eater: document.getElementById('eldritch-operation-eater'),
  corrupted: document.getElementById('eldritch-operation-corrupted'),
  mirrored: document.getElementById('eldritch-operation-mirrored'),
  fractured: document.getElementById('eldritch-operation-fractured'),
  run: document.getElementById('eldritch-operation-run'),
  status: document.getElementById('eldritch-operation-status'),
  result: document.getElementById('eldritch-operation-result'),
};
const eldritchActions = new Set([
  'set_implicit', 'reroll_dominant_side', 'add_dominant_side',
  'remove_dominant_side', 'conflict',
]);
const eldritchSlots = Object.freeze({
  body_armour: '身体装备', boots: '脚部装备', gloves: '手部装备',
  helmet: '头部装备', other: '其他',
});
const eldritchVerdicts = Object.freeze({
  compatible: '已知约束兼容', incompatible: '已知约束不兼容', unknown: '信息不足',
});
const eldritchCheckLabels = Object.freeze({ pass: '通过', fail: '不通过', unknown: '未知' });
const eldritchImplicitLabels = Object.freeze({
  unchanged: '基底词缀不变',
  set_searing_and_replace_all_except_eater: '添加指定档级焚界者基底词缀，保留灭界者基底词缀，替换其他原有基底词缀',
  set_eater_and_replace_all_except_searing: '添加指定档级灭界者基底词缀，保留焚界者基底词缀，替换其他原有基底词缀',
  random_one_raised_other_lowered: '随机提高一方、降低另一方；降低的次级词缀会被移除',
});
const eldritchExplicitLabels = Object.freeze({
  unchanged: '显式词缀不变', prefix_modifiers_rerolled: '重置前缀，后缀保持不变',
  suffix_modifiers_rerolled: '重置后缀，前缀保持不变',
  one_random_prefix_added: '添加一个随机前缀',
  one_random_suffix_added: '添加一个随机后缀',
  one_random_prefix_removed: '移除一个随机前缀',
  one_random_suffix_removed: '移除一个随机后缀',
});
let eldritchEpoch = 0;
let eldritchCatalogEpoch = 0;
let eldritchCatalog = [];
let eldritchCatalogLoaded = false;

function eldritchNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function eldritchSection(title) {
  const section = eldritchNode('section', 'eldritch-operation-section');
  section.append(eldritchNode('h3', '', title));
  return section;
}

function eldritchText(value) {
  return value === null || value === undefined || value === '' ? '未知' : String(value);
}

function eldritchPresence(value) {
  return value === true ? '有' : value === false ? '没有' : '未知';
}

function eldritchSafeUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return '';
    if (['poedb.tw', 'craftofexile.com', 'www.craftofexile.com'].includes(url.hostname)) {
      return url.href;
    }
  } catch (_) { /* 非可信来源只显示文字。 */ }
  return '';
}

function eldritchLink(raw, label) {
  const safe = eldritchSafeUrl(raw);
  if (!safe) return null;
  const link = eldritchNode('a', '', label);
  link.href = safe;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function eldritchSelected() {
  return eldritchCatalog.find(item => item.name_zh === eldritchUi.name.value);
}

function eldritchUpdateRun() {
  eldritchUi.run.disabled = !eldritchCatalogLoaded || !eldritchSelected();
}

function eldritchInvalidate() {
  eldritchEpoch += 1;
  eldritchUi.status.textContent = '';
  eldritchUi.result.replaceChildren();
  eldritchUi.result.hidden = true;
  eldritchUpdateRun();
}

function eldritchInputChanged() {
  eldritchInvalidate();
  eldritchUi.status.textContent = '输入已变化；请重新核对。';
}

function eldritchSyncForm() {
  const chosen = eldritchSelected();
  const action = chosen?.action;
  const isItem = eldritchUi.mode.value === 'item';
  eldritchUi.manual.hidden = isItem;
  eldritchUi.itemWrap.hidden = !isItem;
  eldritchUi.slotWrap.hidden = action !== 'set_implicit';
  eldritchUi.dominantWrap.hidden = !['reroll_dominant_side',
    'add_dominant_side', 'remove_dominant_side'].includes(action);
  eldritchUi.implicitWrap.hidden = !['set_implicit', 'conflict'].includes(action);
  if (!chosen) eldritchUi.actionNote.textContent = '请选择当前入库的古灵通货操作。';
  else if (!chosen.source_contract_ok) {
    eldritchUi.actionNote.textContent = '来源效果原文已变化；旧状态转移不能沿用，结果会保持未知。';
  } else if (isItem && eldritchUi.dominantWrap.hidden === false) {
    eldritchUi.actionNote.textContent = '装备文本不能确定首要方；该操作会保持未知，建议改用手工状态。';
  } else if (action === 'set_implicit') {
    eldritchUi.actionNote.textContent = '余烬/溶液需要装备部位；两方已有基底词缀可明确选择或留空。';
  } else if (action === 'conflict') {
    eldritchUi.actionNote.textContent = '冲突石需要确认两方基底词缀都存在；升降哪一方仍是随机结果。';
  } else {
    eldritchUi.actionNote.textContent = '古灵混沌/崇高/剥离石需要首要方；数量留空保持未知。';
  }
  eldritchInvalidate();
}

async function eldritchError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string') return payload.detail;
    if (typeof payload?.detail?.reason === 'string') return payload.detail.reason;
  } catch (_) { /* 非 JSON 错误使用状态码。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

async function eldritchLoadCatalog() {
  const epoch = ++eldritchCatalogEpoch;
  eldritchCatalog = [];
  eldritchCatalogLoaded = false;
  eldritchUi.name.replaceChildren();
  eldritchUi.name.value = '';
  eldritchUi.catalogStatus.textContent = '正在读取当前入库古灵通货…';
  eldritchUpdateRun();
  try {
    const response = await apiFetch('/craft/eldritch/operations');
    if (epoch !== eldritchCatalogEpoch || !eldritchUi.dialog.open) return;
    if (!response.ok) {
      const message = await eldritchError(response);
      if (epoch === eldritchCatalogEpoch && eldritchUi.dialog.open) {
        eldritchUi.catalogStatus.textContent = message;
      }
      return;
    }
    const data = await response.json();
    if (epoch !== eldritchCatalogEpoch || !eldritchUi.dialog.open) return;
    if (!Array.isArray(data.items) || data.total !== data.items.length || !data.items.length ||
        data.items.some(item => !item || typeof item.name_zh !== 'string' ||
          !eldritchActions.has(item.action) || typeof item.source_contract_ok !== 'boolean')) {
      eldritchUi.catalogStatus.textContent = '没有可用的已审计操作清单。';
      return;
    }
    eldritchCatalog = data.items;
    eldritchCatalogLoaded = true;
    const placeholder = eldritchNode('option', '', '请选择古灵通货');
    placeholder.value = '';
    eldritchUi.name.append(placeholder);
    for (const item of eldritchCatalog) {
      const option = eldritchNode('option', '', item.name_zh +
        (item.source_contract_ok ? '' : '（来源效果已变化）'));
      option.value = item.name_zh;
      eldritchUi.name.append(option);
    }
    eldritchUi.name.value = '';
    eldritchUi.catalogStatus.textContent = `当前入库 ${data.total} 种已审计操作；请明确选择。`;
    eldritchSyncForm();
  } catch (error) {
    if (epoch === eldritchCatalogEpoch && eldritchUi.dialog.open) {
      eldritchUi.catalogStatus.textContent = `清单加载失败：${String(error.message || error)}`;
    }
  }
}

function eldritchOptionalCount(node, label) {
  const raw = node.value.trim();
  if (!raw) return null;
  if (!/^\d+$/.test(raw)) throw new Error(`${label}必须是 0 到 6 的整数。`);
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0 || value > 6) {
    throw new Error(`${label}必须是 0 到 6 的整数。`);
  }
  return value;
}

function eldritchOptionalPresence(node) {
  if (node.value === '') return null;
  if (node.value === 'yes') return true;
  if (node.value === 'no') return false;
  throw new Error('基底词缀状态无效。');
}

function eldritchReadRequest() {
  const selected = eldritchSelected();
  if (!eldritchCatalogLoaded || !selected) {
    throw new Error('请从当前入库清单选择已审计操作。');
  }
  const operation = selected.name_zh;
  if (eldritchUi.mode.value === 'item') {
    const item_text = eldritchUi.item.value.trim();
    if (!item_text || item_text.length > 20_000) {
      throw new Error('完整装备复制文本必须是 1 到 20000 字。');
    }
    return { operation, item_text };
  }
  if (eldritchUi.mode.value !== 'manual') throw new Error('装备状态来源无效。');
  const request = {
    operation, corrupted: eldritchUi.corrupted.checked,
    mirrored: eldritchUi.mirrored.checked, fractured: eldritchUi.fractured.checked,
  };
  if (selected.action === 'set_implicit') {
    const item_slot = eldritchUi.slot.value;
    if (item_slot && !Object.hasOwn(eldritchSlots, item_slot)) {
      throw new Error('装备部位无效。');
    }
    return { ...request, item_slot: item_slot || null,
      has_searing_implicit: eldritchOptionalPresence(eldritchUi.searing),
      has_eater_implicit: eldritchOptionalPresence(eldritchUi.eater) };
  }
  if (selected.action === 'conflict') {
    return { ...request,
      has_searing_implicit: eldritchOptionalPresence(eldritchUi.searing),
      has_eater_implicit: eldritchOptionalPresence(eldritchUi.eater) };
  }
  const rarity = eldritchUi.rarity.value;
  if (rarity && !['普通', '魔法', '稀有', '传奇'].includes(rarity)) {
    throw new Error('稀有度无效。');
  }
  const dominance = eldritchUi.dominance.value;
  if (dominance && !['焚界者', '灭界者', '平衡', '无'].includes(dominance)) {
    throw new Error('首要影响无效。');
  }
  return { ...request, rarity: rarity || null, dominance: dominance || null,
    prefix_count: eldritchOptionalCount(eldritchUi.prefix, '前缀数量'),
    suffix_count: eldritchOptionalCount(eldritchUi.suffix, '后缀数量') };
}

function eldritchRender(data) {
  eldritchUi.result.replaceChildren();
  const checks = Array.isArray(data.checks) ? data.checks : [];
  const sourceKnown = checks.some(check =>
    check.key === 'source_contract' && check.status === 'pass');
  const verdict = eldritchSection('结论');
  verdict.append(eldritchNode('h4', '', eldritchVerdicts[data.verdict] || '信息不足'));
  verdict.append(eldritchNode('p', '',
    '兼容只表示已提供状态通过来源效果与结构化名额检查，不表示完整游戏可执行。'));
  eldritchUi.result.append(verdict);

  const source = eldritchSection(`${eldritchText(data.operation)} · 来源效果`);
  source.append(eldritchNode('p', '', data.effect_text || '来源没有提供效果原文。'));
  source.append(eldritchNode('p', '',
    `来源 ${data.source || '未知'}；抓取 ${data.fetched_at || '未知'}；` +
    `固定来源 ID ${data.metadata_id || '未知'}。`));
  const sourceLink = eldritchLink(data.source_url, '打开 POEDB 来源页');
  if (sourceLink) source.append(sourceLink);
  eldritchUi.result.append(source);

  const before = data.before || {};
  const state = eldritchSection('输入状态与条件后状态');
  state.append(eldritchNode('p', '',
    `输入：${eldritchText(before.rarity)}；部位 ${eldritchSlots[before.item_slot] || '未知'}；` +
    `首要 ${eldritchText(before.dominance)}；前缀 ${eldritchText(before.prefix_count)}；` +
    `后缀 ${eldritchText(before.suffix_count)}。`));
  state.append(eldritchNode('p', '',
    `基底词缀：焚界者 ${eldritchPresence(before.has_searing_implicit)}；` +
    `灭界者 ${eldritchPresence(before.has_eater_implicit)}。`));
  state.append(eldritchNode('p', '',
    `特殊状态：腐化 ${before.corrupted ? '是' : '否'}；镜像 ${before.mirrored ? '是' : '否'}；` +
    `裂隙 ${before.fractured ? '是' : '否'}。`));
  if (before.incomplete_reason) {
    state.append(eldritchNode('p', 'eldritch-operation-warning',
      `装备文本未完整识别：${before.incomplete_reason}`));
  }
  state.append(eldritchNode('h4', '', '条件后状态（仅当操作实际成功执行时）'));
  if (!sourceKnown) {
    state.append(eldritchNode('p', 'eldritch-operation-warning',
      '来源效果原文未通过逐字契约；状态转移未知，不沿用旧操作语义。'));
  } else {
    if (data.verdict === 'incompatible') {
      state.append(eldritchNode('p', 'eldritch-operation-warning',
        '当前已知约束不兼容；以下字段不是可执行结果。'));
    }
    const after = data.after || {};
    state.append(eldritchNode('p', '',
      `基底词缀：${eldritchImplicitLabels[after.implicit_modifier_change] || '未知'}。`));
    state.append(eldritchNode('p', '',
      `显式词缀：${eldritchExplicitLabels[after.explicit_modifier_change] || '未知'}。`));
    state.append(eldritchNode('p', '',
      `前缀 ${eldritchText(after.prefix_count)}；后缀 ${eldritchText(after.suffix_count)}；` +
      `焚界者基底 ${eldritchPresence(after.has_searing_implicit)}；` +
      `灭界者基底 ${eldritchPresence(after.has_eater_implicit)}。`));
    if (after.searing_implicit_tier || after.eater_implicit_tier) {
      state.append(eldritchNode('p', '',
        `新基底档级：焚界者 ${eldritchText(after.searing_implicit_tier)}；` +
        `灭界者 ${eldritchText(after.eater_implicit_tier)}。`));
    }
    if (after.random_outcome) {
      state.append(eldritchNode('p', '',
        '此操作含随机结果；这里不提供具体词缀、升降档级或概率。'));
    }
  }
  eldritchUi.result.append(state);

  const review = eldritchSection('已知约束检查');
  if (!checks.length) review.append(eldritchNode('p', '', '没有可展示的结构化检查。'));
  for (const check of checks) {
    const row = eldritchNode('div', 'eldritch-operation-check');
    row.append(eldritchNode('strong', '',
      `${check.key || '检查'}：${eldritchCheckLabels[check.status] || '未知'}`));
    row.append(eldritchNode('p', '', check.summary || '未提供说明'));
    const link = eldritchLink(check.source_url, '检查来源');
    if (link) row.append(link);
    review.append(row);
  }
  if (Array.isArray(data.unknown_fields) && data.unknown_fields.length) {
    review.append(eldritchNode('h4', '', '仍未知'));
    const list = eldritchNode('ul');
    for (const item of data.unknown_fields) list.append(eldritchNode('li', '', item));
    review.append(list);
  }
  eldritchUi.result.append(review);

  const matrix = Array.isArray(data.metamod_interactions) ? data.metamod_interactions : [];
  if (matrix.length) {
    const section = eldritchSection('Metamod 矩阵：独立来源原值');
    for (const cell of matrix) {
      section.append(eldritchNode('p', '',
        `${cell.guard || '未知 guard'}：${eldritchText(cell.raw_value)}` +
        (cell.note ? `；原始注释：${cell.note}` : '')));
    }
    const link = eldritchLink(matrix[0].source_url, '打开 Craft of Exile 矩阵');
    if (link) section.append(link);
    section.append(eldritchNode('p', '',
      'YES / NO / N/A 只按来源原文披露；不解释外延，也不改写上面的 POEDB 状态转移。'));
    eldritchUi.result.append(section);
  }
  const boundary = eldritchSection('适用边界');
  boundary.append(eldritchNode('p', '', data.boundary ||
    '不生成随机词缀、概率、步骤、成本或制作路线。'));
  eldritchUi.result.append(boundary);
  eldritchUi.result.hidden = false;
}

async function eldritchSubmit(event) {
  event?.preventDefault();
  eldritchInvalidate();
  let request;
  try {
    request = eldritchReadRequest();
  } catch (error) {
    eldritchUi.status.textContent = String(error.message || error);
    return;
  }
  const epoch = eldritchEpoch;
  eldritchUi.run.disabled = true;
  eldritchUi.status.textContent = '正在核对 POEDB 效果与装备状态…';
  try {
    const response = await apiFetch('/craft/eldritch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (epoch !== eldritchEpoch || !eldritchUi.dialog.open) return;
    if (!response.ok) {
      const message = await eldritchError(response);
      if (epoch === eldritchEpoch && eldritchUi.dialog.open) eldritchUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== eldritchEpoch || !eldritchUi.dialog.open) return;
    if (!data.found) {
      eldritchUi.status.textContent = '当前来源未收录该操作；不能按相似名称猜语义。';
      return;
    }
    eldritchRender(data);
    eldritchUi.status.textContent = '核对完成；条件后状态不等于实际制作结果。';
  } catch (error) {
    if (epoch === eldritchEpoch && eldritchUi.dialog.open) {
      eldritchUi.status.textContent = `核对失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === eldritchEpoch) eldritchUpdateRun();
  }
}

function openEldritchOperation(itemText = '') {
  eldritchInvalidate();
  if (itemText) {
    eldritchUi.mode.value = 'item';
    eldritchUi.item.value = String(itemText);
  }
  if (!eldritchUi.dialog.open) eldritchUi.dialog.showModal();
  eldritchSyncForm();
  eldritchLoadCatalog();
  eldritchUi.name.focus();
}

function clearEldritchOperation() {
  eldritchCatalogEpoch += 1;
  eldritchCatalog = [];
  eldritchCatalogLoaded = false;
  eldritchInvalidate();
  eldritchUi.form.reset();
  eldritchUi.name.replaceChildren();
  eldritchUi.name.value = '';
  eldritchUi.catalogStatus.textContent = '';
  eldritchUi.mode.value = 'manual';
  eldritchUi.item.value = '';
  eldritchUi.slot.value = '';
  eldritchUi.rarity.value = '';
  eldritchUi.dominance.value = '';
  eldritchUi.prefix.value = '';
  eldritchUi.suffix.value = '';
  eldritchUi.searing.value = '';
  eldritchUi.eater.value = '';
  eldritchUi.corrupted.checked = false;
  eldritchUi.mirrored.checked = false;
  eldritchUi.fractured.checked = false;
  eldritchSyncForm();
  if (eldritchUi.dialog.open) eldritchUi.dialog.close();
}

window.openEldritchOperation = openEldritchOperation;
window.clearEldritchOperation = clearEldritchOperation;
eldritchUi.open.addEventListener('click', () => openEldritchOperation());
eldritchUi.close.addEventListener('click', () => eldritchUi.dialog.close());
eldritchUi.dialog.addEventListener('close', () => {
  eldritchCatalogEpoch += 1;
  eldritchCatalog = [];
  eldritchCatalogLoaded = false;
  eldritchUi.name.replaceChildren();
  eldritchUi.catalogStatus.textContent = '';
  eldritchInvalidate();
});
eldritchUi.name.addEventListener('change', eldritchSyncForm);
eldritchUi.mode.addEventListener('change', eldritchSyncForm);
eldritchUi.form.addEventListener('submit', eldritchSubmit);
eldritchUi.form.addEventListener('input', eldritchInputChanged);
eldritchUi.form.addEventListener('change', eldritchInputChanged);
