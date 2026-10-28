/* PoB 命名变体浏览器。历史值只从隔离 API 读，不写入聊天或当前装备卡片。 */
'use strict';

const historyUi = {
  open: document.getElementById('unique-history-open'),
  dialog: document.getElementById('unique-history-dialog'),
  close: document.getElementById('history-close'),
  searchForm: document.getElementById('history-search-form'),
  query: document.getElementById('history-query'),
  only: document.getElementById('history-only'),
  status: document.getElementById('history-status'),
  items: document.getElementById('history-items'),
  prev: document.getElementById('history-prev'),
  next: document.getElementById('history-next'),
  page: document.getElementById('history-page'),
  selected: document.getElementById('history-selected'),
  title: document.getElementById('history-selected-title'),
  note: document.getElementById('history-selected-note'),
  compareForm: document.getElementById('history-compare-form'),
  left: document.getElementById('history-left'),
  right: document.getElementById('history-right'),
  detailStatus: document.getElementById('history-detail-status'),
  detail: document.getElementById('history-detail'),
};

const HISTORY_PAGE_SIZE = 20;
const MOD_KIND = Object.freeze({
  implicit: '隐式', explicit: '显式', crafted: '工艺', fractured: '分裂',
});
let historyQuery = '';
let historyOnly = true;
let historyOffset = 0;
let historyTotal = 0;
let historyUid = '';
let historySearchEpoch = 0;
let historyDetailEpoch = 0;

function historyNode(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = String(value);
  return node;
}

function historyMessage(target, message) {
  target.textContent = message;
}

async function historyResponseError(response) {
  try {
    const payload = await response.json();
    if (typeof payload.detail === 'string') return payload.detail;
  } catch (_) { /* HTTP 错误正文可能不是 JSON。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

function historyClearSelection() {
  historyDetailEpoch += 1;
  historyUid = '';
  historyUi.selected.hidden = true;
  historyUi.detail.replaceChildren();
  historyMessage(historyUi.detailStatus, '');
}

function renderHistoryCatalog(data) {
  historyTotal = data.total;
  historyUi.items.replaceChildren();
  if (!data.archive_ready) {
    historyMessage(historyUi.status, '历史变体库尚未导入。请先运行 PoB 唯一装备导入器。');
  } else if (!data.items.length) {
    historyMessage(historyUi.status, '没有匹配的命名变体。可取消“只显示含历史变体”后重试。');
  } else {
    const first = data.offset + 1;
    const last = data.offset + data.items.length;
    historyMessage(historyUi.status, `找到 ${data.total} 件，当前显示 ${first}–${last} 件。`);
  }
  for (const item of data.items) {
    const button = historyNode('button', 'history-item');
    button.type = 'button';
    button.append(historyNode('strong', '', item.name_zh || item.name_en));
    if (item.name_zh) button.append(historyNode('span', 'history-item-en', item.name_en));
    button.append(historyNode('small', '', `${item.base_type_en || '底材未知'} · 历史 ${item.historical_count} / 当前 ${item.current_count}`));
    button.addEventListener('click', () => loadHistoryItem(item));
    historyUi.items.append(button);
  }
  historyUi.prev.disabled = data.offset === 0;
  historyUi.next.disabled = !data.has_more;
  historyUi.page.textContent = data.total ? `${Math.floor(data.offset / data.limit) + 1} / ${Math.ceil(data.total / data.limit)} 页` : '';
}

async function searchHistory() {
  const epoch = ++historySearchEpoch;
  historyClearSelection();
  historyUi.items.replaceChildren();
  historyUi.prev.disabled = true;
  historyUi.next.disabled = true;
  historyUi.page.textContent = '';
  historyMessage(historyUi.status, '正在查询历史变体…');
  const params = new URLSearchParams({
    query: historyQuery, historical_only: String(historyOnly),
    limit: String(HISTORY_PAGE_SIZE), offset: String(historyOffset),
  });
  try {
    const response = await apiFetch(`/unique-variants/search?${params}`);
    if (epoch !== historySearchEpoch || !historyUi.dialog.open) return;
    if (!response.ok) {
      const message = await historyResponseError(response);
      if (epoch === historySearchEpoch && historyUi.dialog.open) {
        historyMessage(historyUi.status, message);
      }
      return;
    }
    const data = await response.json();
    if (epoch !== historySearchEpoch || !historyUi.dialog.open) return;
    renderHistoryCatalog(data);
  } catch (error) {
    if (epoch === historySearchEpoch && historyUi.dialog.open) {
      historyMessage(historyUi.status, `查询失败：${String(error.message || error)}`);
    }
  }
}

function appendHistoryOptions(select, variants, placeholder) {
  select.replaceChildren();
  const empty = historyNode('option', '', placeholder);
  empty.value = '';
  select.append(empty);
  for (const variant of variants) {
    const option = historyNode(
      'option', '', `${variant.index} · ${variant.name}（${variant.label_meaning}）`
    );
    option.value = String(variant.index);
    select.append(option);
  }
  select.value = '';
}

async function loadHistoryItem(item) {
  const epoch = ++historyDetailEpoch;
  historyUid = item.uid;
  historyUi.selected.hidden = false;
  historyUi.title.textContent = `${item.name_zh ? `${item.name_zh} · ` : ''}${item.name_en}`;
  historyMessage(historyUi.note, '正在读取可用变体…');
  historyUi.detail.replaceChildren();
  historyMessage(historyUi.detailStatus, '');
  appendHistoryOptions(historyUi.left, [], '请选择第一个变体');
  appendHistoryOptions(historyUi.right, [], '只查看第一个变体');
  try {
    const params = new URLSearchParams({ name: item.uid });
    const response = await apiFetch(`/unique-variants?${params}`);
    if (epoch !== historyDetailEpoch || !historyUi.dialog.open) return;
    if (!response.ok) {
      const message = await historyResponseError(response);
      if (epoch === historyDetailEpoch && historyUi.dialog.open) {
        historyMessage(historyUi.note, message);
      }
      return;
    }
    const data = await response.json();
    if (epoch !== historyDetailEpoch || !historyUi.dialog.open) return;
    historyMessage(historyUi.note, data.note);
    appendHistoryOptions(historyUi.left, data.variants, '请选择第一个变体');
    appendHistoryOptions(historyUi.right, data.variants, '只查看第一个变体');
    historyUi.selected.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  } catch (error) {
    if (epoch === historyDetailEpoch && historyUi.dialog.open) {
      historyMessage(historyUi.note, `读取失败：${String(error.message || error)}`);
    }
  }
}

function appendHistoryMods(parent, title, mods) {
  const section = historyNode('section', 'history-mod-group');
  section.append(historyNode('h4', '', `${title}（${mods.length}）`));
  if (!mods.length) {
    section.append(historyNode('p', 'history-empty', '无'));
  } else {
    const list = historyNode('ul', 'history-mod-list');
    for (const mod of mods) {
      const row = historyNode('li', '');
      row.append(
        historyNode('span', 'history-mod-kind', MOD_KIND[mod.kind] || mod.kind || '词缀'),
        historyNode('span', '', mod.text || ''),
      );
      list.append(row);
    }
    section.append(list);
  }
  parent.append(section);
}

function appendSourceLink(parent, variant) {
  try {
    const url = new URL(variant.url);
    if (url.protocol !== 'https:') return;
    const link = historyNode('a', 'history-source', `查看 PoB 来源 · ${variant.source_commit.slice(0, 9) || '未钉住 commit'}`);
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    parent.append(link);
  } catch (_) { /* 无效来源 URL 不做成链接。 */ }
}

function renderHistoryVariant(variant) {
  const card = historyNode('section', 'history-variant');
  card.append(
    historyNode('h4', '', `${variant.index} · ${variant.name}`),
    historyNode('p', 'history-variant-meta', `${variant.scope === 'obsolete' ? '历史' : '当前'} · ${variant.label_meaning} · 底材：${variant.base_type_en || '未知（来源无法唯一确定）'}`),
  );
  appendHistoryMods(card, '完整词缀', variant.mods);
  appendSourceLink(card, variant);
  return card;
}

function renderHistoryResult(data, comparing) {
  historyUi.detail.replaceChildren();
  const cards = historyNode('div', 'history-variant-grid');
  if (!comparing) cards.classList.add('single');
  cards.append(renderHistoryVariant(comparing ? data.left : data.variant));
  if (comparing) cards.append(renderHistoryVariant(data.right));
  historyUi.detail.append(cards);
  if (comparing) {
    const change = historyNode('section', 'history-diff');
    const base = data.base_changed === null ? '底材变化：未知（至少一侧底材无法确定）'
      : data.base_changed ? '底材变化：是' : '底材变化：否';
    change.append(historyNode('p', 'history-variant-meta', base));
    appendHistoryMods(change, '两侧共有', data.shared_mods);
    appendHistoryMods(change, '仅第一个变体有', data.removed_mods);
    appendHistoryMods(change, '仅第二个变体有', data.added_mods);
    historyUi.detail.append(change);
  }
  historyUi.detail.append(historyNode('p', 'history-disclaimer', data.note));
  historyMessage(historyUi.detailStatus, comparing ? '已比较指定的两个变体。' : '已显示指定变体的完整词缀。');
}

async function compareHistory(event) {
  event.preventDefault();
  const left = historyUi.left.value;
  const right = historyUi.right.value;
  if (!historyUid || !left) return;
  if (right && left === right) {
    historyMessage(historyUi.detailStatus, '请选择两个不同的变体编号。');
    return;
  }
  const epoch = ++historyDetailEpoch;
  historyUi.detail.replaceChildren();
  historyMessage(historyUi.detailStatus, '正在读取指定变体…');
  const params = new URLSearchParams({ name: historyUid });
  params.set(right ? 'left_index' : 'index', left);
  if (right) params.set('right_index', right);
  const path = right ? '/unique-variants/compare' : '/unique-variants/detail';
  try {
    const response = await apiFetch(`${path}?${params}`);
    if (epoch !== historyDetailEpoch || !historyUi.dialog.open) return;
    if (!response.ok) {
      const message = await historyResponseError(response);
      if (epoch === historyDetailEpoch && historyUi.dialog.open) {
        historyMessage(historyUi.detailStatus, message);
      }
      return;
    }
    const data = await response.json();
    if (epoch !== historyDetailEpoch || !historyUi.dialog.open) return;
    renderHistoryResult(data, Boolean(right));
  } catch (error) {
    if (epoch === historyDetailEpoch && historyUi.dialog.open) {
      historyMessage(historyUi.detailStatus, `读取失败：${String(error.message || error)}`);
    }
  }
}

historyUi.open.addEventListener('click', () => {
  historyUi.dialog.showModal();
  historyQuery = historyUi.query.value.trim();
  historyOnly = historyUi.only.checked;
  historyOffset = 0;
  searchHistory();
  historyUi.query.focus();
});
historyUi.close.addEventListener('click', () => historyUi.dialog.close());
historyUi.dialog.addEventListener('close', () => {
  historySearchEpoch += 1;
  historyClearSelection();
});
historyUi.searchForm.addEventListener('submit', (event) => {
  event.preventDefault();
  historyQuery = historyUi.query.value.trim();
  historyOnly = historyUi.only.checked;
  historyOffset = 0;
  searchHistory();
});
historyUi.only.addEventListener('change', () => {
  historyOnly = historyUi.only.checked;
  historyOffset = 0;
  searchHistory();
});
historyUi.prev.addEventListener('click', () => {
  historyOffset = Math.max(0, historyOffset - HISTORY_PAGE_SIZE);
  searchHistory();
});
historyUi.next.addEventListener('click', () => {
  if (historyOffset + HISTORY_PAGE_SIZE >= historyTotal) return;
  historyOffset += HISTORY_PAGE_SIZE;
  searchHistory();
});
historyUi.compareForm.addEventListener('submit', compareHistory);
