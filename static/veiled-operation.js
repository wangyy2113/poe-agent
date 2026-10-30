/* 加密通货：逐字效果状态核对与隐匿来源词缀目录。 */
'use strict';

const veiledUi = Object.fromEntries([
  'open', 'dialog', 'close', 'form', 'name', 'catalog-status', 'mode',
  'action-note', 'manual', 'item-wrap', 'item', 'rarity', 'class',
  'prefix', 'suffix', 'corrupted', 'mirrored', 'fractured', 'run',
  'status', 'result',
].map(key => [key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()),
  document.getElementById(`veiled-operation-${key}`)]));
const veiledActions = new Set(['remove_one_add_veiled', 'reroll_all_with_veiled']);
const veiledVerdicts = Object.freeze({
  compatible: '已知约束兼容', incompatible: '已知约束不兼容', unknown: '信息不足',
});
const veiledChecks = Object.freeze({ pass: '通过', fail: '不通过', unknown: '未知' });
const veiledChanges = Object.freeze({
  all_modifiers_rerolled_with_one_random_veiled_modifier: '重置全部词缀，并包含一个随机加密词缀',
  one_random_modifier_removed_and_one_random_veiled_modifier_added: '移除一个随机词缀，并添加一个随机加密词缀',
});
const VEILED_PAGE_SIZE = 20;
let veiledEpoch = 0;
let veiledCatalogEpoch = 0;
let veiledCatalog = [];
let veiledCatalogLoaded = false;

function veiledNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function veiledSection(title) {
  const section = veiledNode('section', 'veiled-operation-section');
  section.append(veiledNode('h3', '', title));
  return section;
}

function veiledText(value) {
  return value === null || value === undefined || value === '' ? '未知' : String(value);
}

function veiledSafeUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return '';
    if (['poedb.tw', 'craftofexile.com', 'www.craftofexile.com'].includes(url.hostname)) {
      return url.href;
    }
  } catch (_) { /* 非可信来源只显示文字。 */ }
  return '';
}

function veiledLink(raw, label) {
  const safe = veiledSafeUrl(raw);
  if (!safe) return null;
  const link = veiledNode('a', '', label);
  link.href = safe;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  return link;
}

function veiledSelected() {
  return veiledCatalog.find(item => item.name_zh === veiledUi.name.value);
}

function veiledUpdateRun() {
  veiledUi.run.disabled = !veiledCatalogLoaded || !veiledSelected();
}

function veiledInvalidate() {
  veiledEpoch += 1;
  veiledUi.status.textContent = '';
  veiledUi.result.replaceChildren();
  veiledUi.result.hidden = true;
  veiledUpdateRun();
}

function veiledInputChanged() {
  veiledInvalidate();
  veiledUi.status.textContent = '输入已变化；请重新核对。';
}

function veiledSyncForm() {
  const chosen = veiledSelected();
  const isItem = veiledUi.mode.value === 'item';
  veiledUi.manual.hidden = isItem;
  veiledUi.itemWrap.hidden = !isItem;
  if (!chosen) veiledUi.actionNote.textContent = '请选择当前入库的加密通货。';
  else if (!chosen.source_contract_ok) {
    veiledUi.actionNote.textContent = '来源效果原文已变化；旧状态转移不能沿用，结果会保持未知。';
  } else if (chosen.action === 'remove_one_add_veiled') {
    veiledUi.actionNote.textContent = '加密崇高石需确认可移除词缀；前后缀数量留空时保持未知。';
  } else {
    veiledUi.actionNote.textContent = '加密混沌石按原文重置词缀；具体结果仍是随机的。';
  }
  veiledInvalidate();
}

async function veiledError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === 'string') return payload.detail;
    if (typeof payload?.detail?.reason === 'string') return payload.detail.reason;
  } catch (_) { /* 非 JSON 错误使用状态码。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

async function veiledLoadCatalog() {
  const epoch = ++veiledCatalogEpoch;
  veiledCatalog = [];
  veiledCatalogLoaded = false;
  veiledUi.name.replaceChildren();
  veiledUi.name.value = '';
  veiledUi.catalogStatus.textContent = '正在读取当前入库加密通货…';
  veiledUpdateRun();
  try {
    const response = await apiFetch('/craft/veiled/operations');
    if (epoch !== veiledCatalogEpoch || !veiledUi.dialog.open) return;
    if (!response.ok) {
      const message = await veiledError(response);
      if (epoch === veiledCatalogEpoch && veiledUi.dialog.open) {
        veiledUi.catalogStatus.textContent = message;
      }
      return;
    }
    const data = await response.json();
    if (epoch !== veiledCatalogEpoch || !veiledUi.dialog.open) return;
    if (!Array.isArray(data.items) || data.total !== data.items.length || !data.items.length ||
        data.items.some(item => !item || typeof item.name_zh !== 'string' ||
          !veiledActions.has(item.action) || typeof item.source_contract_ok !== 'boolean')) {
      veiledUi.catalogStatus.textContent = '没有可用的已审计操作清单。';
      return;
    }
    veiledCatalog = data.items;
    veiledCatalogLoaded = true;
    const placeholder = veiledNode('option', '', '请选择加密通货');
    placeholder.value = '';
    veiledUi.name.append(placeholder);
    for (const item of veiledCatalog) {
      const option = veiledNode('option', '', item.name_zh +
        (item.source_contract_ok ? '' : '（来源效果已变化）'));
      option.value = item.name_zh;
      veiledUi.name.append(option);
    }
    veiledUi.name.value = '';
    veiledUi.catalogStatus.textContent = `当前入库 ${data.total} 种已审计操作；请明确选择。`;
    veiledSyncForm();
  } catch (error) {
    if (epoch === veiledCatalogEpoch && veiledUi.dialog.open) {
      veiledUi.catalogStatus.textContent = `清单加载失败：${String(error.message || error)}`;
    }
  }
}

function veiledCount(node, label) {
  const raw = node.value.trim();
  if (!raw) return null;
  if (!/^\d+$/.test(raw) || Number(raw) > 6) {
    throw new Error(`${label}必须是 0 到 6 的整数。`);
  }
  return Number(raw);
}

function veiledReadRequest() {
  const chosen = veiledSelected();
  if (!veiledCatalogLoaded || !chosen) throw new Error('请从当前入库清单选择已审计操作。');
  const operation = chosen.name_zh;
  if (veiledUi.mode.value === 'item') {
    const item_text = veiledUi.item.value.trim();
    if (!item_text || item_text.length > 20_000) {
      throw new Error('完整装备复制文本必须是 1 到 20000 字。');
    }
    return { operation, item_text };
  }
  if (veiledUi.mode.value !== 'manual') throw new Error('装备状态来源无效。');
  const rarity = veiledUi.rarity.value;
  if (rarity && !['普通', '魔法', '稀有', '传奇'].includes(rarity)) {
    throw new Error('稀有度无效。');
  }
  const item_class = veiledUi.class.value.trim();
  if (item_class.length > 80) throw new Error('底材类别不能超过 80 字。');
  return {
    operation, rarity: rarity || null, item_class: item_class || null,
    prefix_count: veiledCount(veiledUi.prefix, '前缀数量'),
    suffix_count: veiledCount(veiledUi.suffix, '后缀数量'),
    corrupted: veiledUi.corrupted.checked, mirrored: veiledUi.mirrored.checked,
    fractured: veiledUi.fractured.checked,
  };
}

function veiledRenderPool(pool) {
  const section = veiledSection('隐匿来源词缀候选目录');
  if (!pool) {
    section.append(veiledNode('p', '', '未取得可解析的底材类别目录。'));
    return section;
  }
  section.append(veiledNode('p', '',
    `${veiledText(pool.item_class)}：来源 ${veiledText(pool.source_rows)} 行；` +
    `去重 ${veiledText(pool.unique_candidates)} 条；前缀 ${veiledText(pool.prefix_candidates)} 条；` +
    `后缀 ${veiledText(pool.suffix_candidates)} 条；覆盖 ${veiledText(pool.scope)}。`));
  section.append(veiledNode('p', 'veiled-operation-warning', pool.caveat ||
    '目录不是单次揭露选项；页面 weight 不等于操作结果概率。'));
  const candidates = Array.isArray(pool.candidates) ? pool.candidates : [];
  if (candidates.length !== pool.unique_candidates) {
    section.append(veiledNode('p', 'veiled-operation-warning',
      `目录计数与返回条目不一致：收到 ${candidates.length} 条，请核对来源数据。`));
  }
  const list = veiledNode('div');
  const controls = veiledNode('div', 'veiled-operation-pages');
  let page = 0;
  const renderPage = () => {
    list.replaceChildren();
    controls.replaceChildren();
    const pages = Math.ceil(candidates.length / VEILED_PAGE_SIZE);
    const start = page * VEILED_PAGE_SIZE;
    for (const candidate of candidates.slice(start, start + VEILED_PAGE_SIZE)) {
      const row = veiledNode('div', 'veiled-operation-check');
      row.append(veiledNode('strong', '',
        `${veiledText(candidate.position)} · ${veiledText(candidate.name_zh)} · ${veiledText(candidate.group)}`));
      row.append(veiledNode('p', '', veiledText(candidate.text)));
      row.append(veiledNode('p', '',
        `物等 ${veiledText(candidate.item_level)}；页面生成权重 ${veiledText(candidate.weight)}；` +
        `来源代码 ${veiledText(candidate.mod_code)}`));
      const link = veiledLink(candidate.url, '词缀来源页');
      if (link) row.append(link);
      list.append(row);
    }
    if (pages > 1) {
      const previous = veiledNode('button', '', '上一页');
      previous.type = 'button';
      previous.disabled = page === 0;
      previous.addEventListener('click', () => { page -= 1; renderPage(); });
      const next = veiledNode('button', '', '下一页');
      next.type = 'button';
      next.disabled = page === pages - 1;
      next.addEventListener('click', () => { page += 1; renderPage(); });
      controls.append(previous, veiledNode('span', '',
        `第 ${page + 1} / ${pages} 页，共 ${candidates.length} 条`), next);
    }
  };
  renderPage();
  section.append(list, controls);
  return section;
}

function veiledRender(data) {
  veiledUi.result.replaceChildren();
  const checks = Array.isArray(data.checks) ? data.checks : [];
  const sourceKnown = checks.some(check =>
    check.key === 'source_contract' && check.status === 'pass');
  const verdict = veiledSection('结论');
  verdict.append(veiledNode('h4', '', veiledVerdicts[data.verdict] || '信息不足'));
  verdict.append(veiledNode('p', '',
    '兼容只表示已提供状态通过来源效果中的已知前提，不表示完整游戏可执行。'));
  veiledUi.result.append(verdict);

  const source = veiledSection(`${veiledText(data.operation)} · 来源效果`);
  source.append(veiledNode('p', '', data.effect_text || '来源没有提供效果原文。'));
  source.append(veiledNode('p', '',
    `来源 ${veiledText(data.source)}；抓取 ${veiledText(data.fetched_at)}；` +
    `固定来源 ID ${veiledText(data.metadata_id)}。`));
  const sourceLink = veiledLink(data.source_url, '打开 POEDB 来源页');
  if (sourceLink) source.append(sourceLink);
  veiledUi.result.append(source);

  const before = data.before || {};
  const state = veiledSection('输入状态与条件后状态');
  state.append(veiledNode('p', '',
    `输入：${veiledText(before.rarity)}；类别 ${veiledText(before.item_class)}；` +
    `前缀 ${veiledText(before.prefix_count)}；后缀 ${veiledText(before.suffix_count)}。`));
  state.append(veiledNode('p', '',
    `特殊状态：腐化 ${before.corrupted ? '是' : '否'}；镜像 ${before.mirrored ? '是' : '否'}；` +
    `裂隙 ${before.fractured ? '是' : '否'}。`));
  if (before.incomplete_reason) {
    state.append(veiledNode('p', 'veiled-operation-warning',
      `装备文本未完整识别：${before.incomplete_reason}`));
  }
  state.append(veiledNode('h4', '', '条件后状态（仅当操作实际成功执行时）'));
  if (!sourceKnown) {
    state.append(veiledNode('p', 'veiled-operation-warning',
      '来源效果原文未通过逐字契约；状态转移未知，不沿用旧操作语义。'));
  } else {
    if (data.verdict === 'incompatible') {
      state.append(veiledNode('p', 'veiled-operation-warning',
        '当前已知约束不兼容；以下字段不是可执行结果。'));
    }
    const after = data.after || {};
    state.append(veiledNode('p', '',
      `词缀变化：${veiledChanges[after.modifier_change] || '未知'}。`));
    state.append(veiledNode('p', '',
      `稀有度 ${veiledText(after.rarity)}；前缀 ${veiledText(after.prefix_count)}；` +
      `后缀 ${veiledText(after.suffix_count)}；加密词缀 ${after.has_veiled_modifier === true ? '有' : '未知'}。`));
    const positions = Array.isArray(after.veiled_modifier_position) ? after.veiled_modifier_position : [];
    state.append(veiledNode('p', '',
      `加密词缀可能位置：${positions.length ? positions.join(' / ') : '未知'}。`));
    const counts = Array.isArray(after.possible_affix_counts) ? after.possible_affix_counts : [];
    if (counts.length) state.append(veiledNode('p', '',
      `可能的前后缀计数：${counts.map(item =>
        `${veiledText(item.prefix_count)} 前缀 + ${veiledText(item.suffix_count)} 后缀`).join('；')}。`));
    if (after.random_outcome) state.append(veiledNode('p', '',
      '此操作含随机结果；具体词缀、揭露选项与概率未知。'));
  }
  veiledUi.result.append(state);

  const review = veiledSection('已知约束检查');
  for (const check of checks) {
    const row = veiledNode('div', 'veiled-operation-check');
    row.append(veiledNode('strong', '',
      `${check.key || '检查'}：${veiledChecks[check.status] || '未知'}`));
    row.append(veiledNode('p', '', check.summary || '未提供说明'));
    const link = veiledLink(check.source_url, '检查来源');
    if (link) row.append(link);
    review.append(row);
  }
  if (Array.isArray(data.unknown_fields) && data.unknown_fields.length) {
    review.append(veiledNode('h4', '', '仍未知'));
    const list = veiledNode('ul');
    for (const item of data.unknown_fields) list.append(veiledNode('li', '', item));
    review.append(list);
  }
  veiledUi.result.append(review);
  veiledUi.result.append(veiledRenderPool(data.veiled_pool));

  const matrix = Array.isArray(data.metamod_interactions) ? data.metamod_interactions : [];
  if (matrix.length) {
    const section = veiledSection('Metamod 矩阵：独立来源原值');
    for (const cell of matrix) section.append(veiledNode('p', '',
      `${cell.guard || '未知 guard'}：${veiledText(cell.raw_value)}` +
      (cell.note ? `；原始注释：${cell.note}` : '')));
    const link = veiledLink(matrix[0].source_url, '打开 Craft of Exile 矩阵');
    if (link) section.append(link);
    section.append(veiledNode('p', '',
      'YES / NO / N/A 只按来源原文披露；不解释外延，也不改写上面的 POEDB 状态转移。'));
    veiledUi.result.append(section);
  }
  const boundary = veiledSection('适用边界');
  boundary.append(veiledNode('p', '', data.boundary ||
    '不生成随机词缀、概率、步骤、成本或制作路线。'));
  veiledUi.result.append(boundary);
  veiledUi.result.hidden = false;
}

async function veiledSubmit(event) {
  event?.preventDefault();
  veiledInvalidate();
  let request;
  try { request = veiledReadRequest(); }
  catch (error) { veiledUi.status.textContent = String(error.message || error); return; }
  const epoch = veiledEpoch;
  veiledUi.run.disabled = true;
  veiledUi.status.textContent = '正在核对 POEDB 效果与装备状态…';
  try {
    const response = await apiFetch('/craft/veiled', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (epoch !== veiledEpoch || !veiledUi.dialog.open) return;
    if (!response.ok) {
      const message = await veiledError(response);
      if (epoch === veiledEpoch && veiledUi.dialog.open) veiledUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== veiledEpoch || !veiledUi.dialog.open) return;
    if (!data.found) {
      veiledUi.status.textContent = '当前来源未收录该操作；不能按相似名称猜语义。';
      return;
    }
    veiledRender(data);
    veiledUi.status.textContent = '核对完成；条件后状态不等于实际制作结果。';
  } catch (error) {
    if (epoch === veiledEpoch && veiledUi.dialog.open) {
      veiledUi.status.textContent = `核对失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === veiledEpoch) veiledUpdateRun();
  }
}

function openVeiledOperation(itemText = '') {
  veiledInvalidate();
  if (itemText) {
    veiledUi.mode.value = 'item';
    veiledUi.item.value = String(itemText);
  }
  if (!veiledUi.dialog.open) veiledUi.dialog.showModal();
  veiledSyncForm();
  veiledLoadCatalog();
  veiledUi.name.focus();
}

function clearVeiledOperation() {
  veiledCatalogEpoch += 1;
  veiledCatalog = [];
  veiledCatalogLoaded = false;
  veiledInvalidate();
  veiledUi.form.reset();
  veiledUi.name.replaceChildren();
  veiledUi.name.value = '';
  veiledUi.catalogStatus.textContent = '';
  veiledUi.mode.value = 'manual';
  for (const key of ['item', 'rarity', 'class', 'prefix', 'suffix']) veiledUi[key].value = '';
  for (const key of ['corrupted', 'mirrored', 'fractured']) veiledUi[key].checked = false;
  veiledSyncForm();
  if (veiledUi.dialog.open) veiledUi.dialog.close();
}

window.openVeiledOperation = openVeiledOperation;
window.clearVeiledOperation = clearVeiledOperation;
veiledUi.open.addEventListener('click', () => openVeiledOperation());
veiledUi.close.addEventListener('click', () => veiledUi.dialog.close());
veiledUi.dialog.addEventListener('close', () => {
  veiledCatalogEpoch += 1;
  veiledCatalog = [];
  veiledCatalogLoaded = false;
  veiledUi.name.replaceChildren();
  veiledUi.catalogStatus.textContent = '';
  veiledInvalidate();
});
veiledUi.name.addEventListener('change', veiledSyncForm);
veiledUi.mode.addEventListener('change', veiledSyncForm);
veiledUi.form.addEventListener('submit', veiledSubmit);
veiledUi.form.addEventListener('input', veiledInputChanged);
veiledUi.form.addEventListener('change', veiledInputChanged);
