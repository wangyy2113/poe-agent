/* 跨来源核对台账。review 永远只表示待核对差异。 */
'use strict';

const conflictUi = {
  open: document.getElementById('conflict-open'),
  dialog: document.getElementById('conflict-dialog'),
  close: document.getElementById('conflict-close'),
  form: document.getElementById('conflict-form'),
  query: document.getElementById('conflict-query'),
  statusFilter: document.getElementById('conflict-kind-status'),
  resolution: document.getElementById('conflict-resolution'),
  kind: document.getElementById('conflict-kind'),
  source: document.getElementById('conflict-source'),
  search: document.getElementById('conflict-search'),
  summaryStatus: document.getElementById('conflict-summary-status'),
  summary: document.getElementById('conflict-summary'),
  status: document.getElementById('conflict-status'),
  results: document.getElementById('conflict-results'),
  prev: document.getElementById('conflict-prev'),
  next: document.getElementById('conflict-next'),
  page: document.getElementById('conflict-page'),
  detail: document.getElementById('conflict-detail'),
};

let conflictSummaryEpoch = 0;
let conflictListEpoch = 0;
let conflictDetailEpoch = 0;
let conflictFilters = null;
let conflictPageCursors = [null];
let conflictPageIndex = 0;
let conflictLastPage = null;

function conflictNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function conflictSafeUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return '';
    if ([
      'poedb.tw', 'www.poewiki.net', 'github.com', 'raw.githubusercontent.com',
      'www.pathofexile.com',
    ].includes(url.hostname)) return url.href;
  } catch (_) { /* 来源没有可用外链。 */ }
  return '';
}

function conflictLabel(item) {
  return item.status === 'review' ? '待核对差异' : '已确认冲突';
}

function conflictPreview(value) {
  const text = String(value ?? '');
  return text.length > 180 ? `${text.slice(0, 179)}…` : text;
}

function conflictClearDetail() {
  conflictDetailEpoch += 1;
  conflictUi.detail.hidden = true;
  conflictUi.detail.replaceChildren();
}

function conflictClearList() {
  conflictListEpoch += 1;
  conflictClearDetail();
  conflictUi.results.replaceChildren();
  conflictUi.status.textContent = '';
  conflictUi.page.textContent = '';
  conflictUi.prev.disabled = true;
  conflictUi.next.disabled = true;
  conflictLastPage = null;
}

function conflictFiltersChanged() {
  conflictFilters = null;
  conflictPageCursors = [null];
  conflictPageIndex = 0;
  conflictClearList();
}

function conflictReadFilters() {
  const kind = conflictUi.kind.value.trim();
  const source = conflictUi.source.value.trim();
  return {
    query: conflictUi.query.value.trim(),
    status: conflictUi.statusFilter.value,
    resolution: conflictUi.resolution.value,
    kinds: kind ? [kind] : [],
    sources: source ? [source] : [],
    limit: 20,
  };
}

async function conflictError(response) {
  try {
    const payload = await response.json();
    const detail = payload?.detail;
    if (typeof detail === 'string') return detail;
    if (typeof detail?.reason === 'string') return detail.reason;
  } catch (_) { /* 非 JSON 错误以状态码说明。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

async function conflictLoadSummary() {
  const epoch = ++conflictSummaryEpoch;
  conflictUi.summaryStatus.textContent = '正在读取台账概览…';
  conflictUi.summary.replaceChildren();
  try {
    const response = await apiFetch('/conflicts/summary');
    if (epoch !== conflictSummaryEpoch || !conflictUi.dialog.open) return;
    if (!response.ok) {
      const message = await conflictError(response);
      if (epoch === conflictSummaryEpoch && conflictUi.dialog.open) conflictUi.summaryStatus.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== conflictSummaryEpoch || !conflictUi.dialog.open) return;
    const counts = conflictNode('div', 'conflict-counts');
    for (const [label, value] of [
      ['未解决的已确认冲突', data.confirmed_active_count],
      ['未解决的待核对差异', data.review_active_count],
      ['全部已确认冲突', data.confirmed_count],
      ['全部待核对差异', data.review_count],
    ]) {
      const card = conflictNode('div', 'conflict-count');
      card.append(conflictNode('strong', '', value ?? '未提供'), conflictNode('span', '', label));
      counts.append(card);
    }
    conflictUi.summary.append(counts);
    const causes = Array.isArray(data.review_causes) ? data.review_causes : [];
    if (causes.length) {
      conflictUi.summary.append(conflictNode('p', 'conflict-causes',
        `待核对成因：${causes.map(row => `${row.cause || '未标注'} ${row.review_count ?? 0} 条`).join('；')}`));
    }
    conflictUi.summaryStatus.textContent = '';
  } catch (error) {
    if (epoch === conflictSummaryEpoch && conflictUi.dialog.open) {
      conflictUi.summaryStatus.textContent = `概览读取失败：${String(error.message || error)}`;
    }
  }
}

function conflictRenderList(data) {
  conflictUi.results.replaceChildren();
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    conflictUi.results.append(conflictNode('p', 'conflict-meta', '当前条件没有匹配记录。'));
  }
  for (const item of items) {
    const card = conflictNode('article', 'conflict-item');
    const badge = conflictNode('span', `conflict-badge ${item.status === 'review' ? 'review' : ''}`,
      conflictLabel(item));
    card.append(badge);
    if (item.resolved) card.append(conflictNode('span', 'conflict-badge', '已解决 · 历史'));
    card.append(
      conflictNode('h3', '', `${item.subject || '未知对象'} · ${item.field || '未知字段'}`),
      conflictNode('p', 'conflict-meta', `#${item.conflict_id} · ${item.kind || '未知 kind'} · ${item.cause || '未标注成因'}`),
      conflictNode('p', 'conflict-preview', `${item.source_a || '来源 A'}：${conflictPreview(item.value_a)}`),
      conflictNode('p', 'conflict-preview', `${item.source_b || '来源 B'}：${conflictPreview(item.value_b)}`)
    );
    const button = conflictNode('button', 'ghost', '查看两侧原值');
    button.type = 'button';
    button.addEventListener('click', () => conflictLoadDetail(item.conflict_id));
    card.append(button);
    conflictUi.results.append(card);
  }
  conflictUi.page.textContent = `第 ${conflictPageIndex + 1} 页 · 本页 ${items.length} 条 / 匹配 ${data.total_matching ?? 0} 条`;
  conflictUi.prev.disabled = conflictPageIndex === 0;
  conflictUi.next.disabled = !data.has_more || !data.next_cursor;
}

async function conflictLoadPage() {
  if (!conflictFilters) return;
  const epoch = ++conflictListEpoch;
  conflictClearDetail();
  conflictUi.results.replaceChildren();
  conflictUi.status.textContent = '正在查询台账…';
  conflictUi.prev.disabled = true;
  conflictUi.next.disabled = true;
  try {
    const response = await apiFetch('/conflicts/lookup', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...conflictFilters, cursor: conflictPageCursors[conflictPageIndex] }),
    });
    if (epoch !== conflictListEpoch || !conflictUi.dialog.open) return;
    if (!response.ok) {
      const message = await conflictError(response);
      if (epoch === conflictListEpoch && conflictUi.dialog.open) conflictUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== conflictListEpoch || !conflictUi.dialog.open) return;
    conflictLastPage = data;
    conflictRenderList(data);
    conflictUi.status.textContent = data.review_count
      ? `本页有 ${data.review_count} 条待核对差异，不能视为已确认冲突。`
      : '查询完成。';
  } catch (error) {
    if (epoch === conflictListEpoch && conflictUi.dialog.open) {
      conflictUi.status.textContent = `查询失败：${String(error.message || error)}`;
    }
  }
}

function conflictSearch(event) {
  event?.preventDefault();
  conflictFilters = conflictReadFilters();
  conflictPageCursors = [null];
  conflictPageIndex = 0;
  conflictLoadPage();
}

function conflictSide(label, source, value, ref, url, length, truncated) {
  const side = conflictNode('div', 'conflict-side');
  side.append(conflictNode('h4', '', `${label} · ${source || '未知来源'}`));
  side.append(conflictNode('pre', '', value ?? ''));
  if (truncated) side.append(conflictNode('p', 'conflict-meta',
    `原值共 ${length} 字，单次详情只显示前 16000 字。`));
  if (ref) side.append(conflictNode('p', 'conflict-meta', `来源标识：${ref}`));
  const safe = conflictSafeUrl(url);
  if (safe) {
    const link = conflictNode('a', '', '打开来源页面');
    link.href = safe;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    side.append(link);
  }
  return side;
}

function conflictRenderDetail(item) {
  const detail = conflictUi.detail;
  detail.replaceChildren();
  detail.append(conflictNode('h3', '', `#${item.conflict_id} · ${item.subject} · ${item.field}`));
  detail.append(conflictNode('p', 'conflict-meta',
    `${conflictLabel(item)} · ${item.resolved ? '已解决（历史）' : '未解决'} · ${item.kind} · ` +
    `成因 ${item.cause || '未标注'} · 首次 ${item.first_seen || '未提供'} · 最近 ${item.last_seen || '未提供'}`));
  const grid = conflictNode('div', 'conflict-detail-grid');
  grid.append(
    conflictSide('来源 A', item.source_a, item.value_a, item.ref_a, item.url_a,
      item.value_a_length, item.value_a_truncated),
    conflictSide('来源 B', item.source_b, item.value_b, item.ref_b, item.url_b,
      item.value_b_length, item.value_b_truncated)
  );
  detail.append(grid);
  if (item.note) detail.append(conflictNode('p', 'conflict-meta', `核对备注：${item.note}`));
  detail.append(conflictNode('p', 'conflict-boundary', item.boundary ||
    '台账只展示两侧原值；待核对差异不构成已确认冲突。'));
  detail.hidden = false;
}

async function conflictLoadDetail(conflictId) {
  const epoch = ++conflictDetailEpoch;
  const listEpoch = conflictListEpoch;
  conflictUi.detail.replaceChildren(conflictNode('p', 'conflict-meta', '正在读取单条详情…'));
  conflictUi.detail.hidden = false;
  try {
    const response = await apiFetch(`/conflicts/${encodeURIComponent(conflictId)}`);
    if (epoch !== conflictDetailEpoch || listEpoch !== conflictListEpoch || !conflictUi.dialog.open) return;
    if (!response.ok) {
      const message = await conflictError(response);
      if (epoch === conflictDetailEpoch && listEpoch === conflictListEpoch && conflictUi.dialog.open) {
        conflictUi.detail.replaceChildren(conflictNode('p', 'conflict-meta', message));
      }
      return;
    }
    const data = await response.json();
    if (epoch !== conflictDetailEpoch || listEpoch !== conflictListEpoch || !conflictUi.dialog.open) return;
    conflictRenderDetail(data);
  } catch (error) {
    if (epoch === conflictDetailEpoch && listEpoch === conflictListEpoch && conflictUi.dialog.open) {
      conflictUi.detail.replaceChildren(conflictNode('p', 'conflict-meta', `详情读取失败：${String(error.message || error)}`));
    }
  }
}

function openConflictLedger() {
  conflictClearList();
  if (!conflictUi.dialog.open) conflictUi.dialog.showModal();
  conflictLoadSummary();
  conflictSearch();
  conflictUi.query.focus();
}

function clearConflictLedger() {
  conflictSummaryEpoch += 1;
  conflictClearList();
  conflictUi.form.reset();
  conflictUi.query.value = '';
  conflictUi.kind.value = '';
  conflictUi.source.value = '';
  conflictUi.statusFilter.value = 'confirmed';
  conflictUi.resolution.value = 'active';
  conflictUi.summaryStatus.textContent = '';
  conflictUi.summary.replaceChildren();
  conflictFilters = null;
  conflictPageCursors = [null];
  conflictPageIndex = 0;
  if (conflictUi.dialog.open) conflictUi.dialog.close();
}

window.clearConflictLedger = clearConflictLedger;
conflictUi.open.addEventListener('click', openConflictLedger);
conflictUi.close.addEventListener('click', () => conflictUi.dialog.close());
conflictUi.dialog.addEventListener('close', () => {
  if (!conflictUi.dialog.open) { conflictSummaryEpoch += 1; conflictClearList(); }
});
conflictUi.form.addEventListener('submit', conflictSearch);
conflictUi.form.addEventListener('input', conflictFiltersChanged);
conflictUi.form.addEventListener('change', conflictFiltersChanged);
conflictUi.prev.addEventListener('click', () => {
  if (conflictPageIndex > 0) { conflictPageIndex -= 1; conflictLoadPage(); }
});
conflictUi.next.addEventListener('click', () => {
  if (!conflictLastPage?.has_more || !conflictLastPage.next_cursor) return;
  conflictPageCursors = conflictPageCursors.slice(0, conflictPageIndex + 1);
  conflictPageCursors.push(conflictLastPage.next_cursor);
  conflictPageIndex += 1;
  conflictLoadPage();
});
