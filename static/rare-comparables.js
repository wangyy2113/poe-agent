/* 稀有装备可比挂单。只渲染结构化 API 结果；用户确认后才触发 Trade 查询。 */
'use strict';

const rareUi = {
  open: document.getElementById('rare-open'),
  dialog: document.getElementById('rare-dialog'),
  close: document.getElementById('rare-close'),
  form: document.getElementById('rare-form'),
  text: document.getElementById('rare-text'),
  league: document.getElementById('rare-league'),
  quality: document.getElementById('rare-quality'),
  sockets: document.getElementById('rare-sockets'),
  submit: document.getElementById('rare-submit'),
  status: document.getElementById('rare-status'),
  result: document.getElementById('rare-result'),
};

let rareEpoch = 0;

function rareNode(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(text);
  return node;
}

function rareField(label, value) {
  const row = rareNode('div', 'rare-field');
  row.append(rareNode('dt', '', label), rareNode('dd', '', value));
  return row;
}

function rareSection(title) {
  const section = rareNode('section', 'rare-section');
  section.append(rareNode('h3', '', title));
  return section;
}

function rareFormatNumber(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)
    : '未提供';
}

function rareSafeTradeUrl(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol === 'https:' && url.hostname === 'www.pathofexile.com'
        && !url.username && !url.password && !url.port
        && url.pathname.startsWith('/trade/search/')) return url.href;
  } catch (_) { /* 缺少可用搜索链接时只展示来源文字。 */ }
  return '';
}

function rareMapping(data) {
  const section = rareSection('词缀映射');
  const quality = { full: '完整', partial: '部分', none: '无' }[data.comparison_quality]
    || '未知';
  section.append(rareNode(
    'p', 'rare-muted',
    `${data.mapped_mod_count ?? 0} / ${data.explicit_mod_count ?? 0} 条显式词缀已映射；映射完整度：${quality}`
  ));
  const mapped = Array.isArray(data.mapped_stats) ? data.mapped_stats : [];
  if (mapped.length) {
    const list = rareNode('ul', 'rare-mod-list');
    for (const mod of mapped) {
      const row = rareNode('li');
      row.append(rareNode('strong', '', mod.mod_text || '未知词缀'));
      const official = Array.isArray(mod.official_texts) ? mod.official_texts : [];
      row.append(rareNode('small', '', `官方 Trade stat：${official.join(' / ') || '未提供'}`));
      list.append(row);
    }
    section.append(list);
  }
  const unmapped = Array.isArray(data.unmapped_mods) ? data.unmapped_mods : [];
  if (unmapped.length) {
    section.append(rareNode('p', 'rare-warning', '以下词缀未映射，挂单只能部分可比：'));
    const list = rareNode('ul', 'rare-mod-list');
    for (const mod of unmapped) list.append(rareNode('li', '', mod));
    section.append(list);
  }
  return section;
}

function rareQuote(data) {
  const section = rareSection('在线挂单样本');
  const quote = data.quote;
  if (!quote) {
    section.append(rareNode('p', 'rare-warning', '没有可查询的官方显式 stat，不输出代表价。'));
    return section;
  }
  const headline = rareNode('div', 'rare-price');
  if (data.available === true && quote.median_chaos != null) {
    headline.append(
      rareNode('strong', '', `${rareFormatNumber(quote.median_chaos)} 混沌石`),
      rareNode('span', '', '可比挂单中位数')
    );
    section.append(headline);
    section.append(rareNode(
      'p', 'rare-muted',
      `样本范围 ${rareFormatNumber(quote.min_chaos)}–${rareFormatNumber(quote.max_chaos)} 混沌石。`
    ));
  } else {
    headline.append(rareNode('strong', '', '样本不足，不输出代表价'));
    section.append(headline);
  }
  const counts = rareNode('dl', 'rare-grid');
  counts.append(
    rareField('官方搜索命中', quote.search_total ?? quote.raw_listing_count ?? '未提供'),
    rareField('本次抓取', quote.fetched_listing_count ?? '未提供'),
    rareField('过滤后样本', `${quote.sample_count ?? 0} / 至少 ${quote.minimum_sample_count ?? 5}`),
    rareField('Chaos / Divine 标价', `${quote.chaos_listing_count ?? 0} / ${quote.divine_listing_count ?? 0}`),
    rareField('最早挂单', quote.earliest_listing_at || '未提供'),
    rareField('最新挂单', quote.latest_listing_at || '未提供')
  );
  section.append(counts);
  section.append(rareNode('p', 'rare-muted', `数据抓取时间：${data.fetched_at || '未提供'}`));
  section.append(rareNode('p', 'rare-scope', quote.sample_scope || '样本范围未提供'));
  return section;
}

function rareAudit(data) {
  const quote = data.quote;
  if (!quote) return null;
  const section = rareSection('过滤审计');
  const list = rareNode('dl', 'rare-grid');
  const fields = [
    ['跨联盟', 'cross_league_filtered_count'],
    ['身份或报价无效', 'invalid_quote_filtered_count'],
    ['品质不符', 'quality_mismatch_filtered_count'],
    ['连接组不符', 'socket_mismatch_filtered_count'],
    ['结构字段缺失', 'structure_missing_filtered_count'],
    ['无法换算货币', 'unconverted_currency_filtered_count'],
    ['统计离群', 'outlier_filtered_count'],
  ];
  for (const [label, key] of fields) list.append(rareField(label, quote[key] ?? 0));
  section.append(list);
  return section;
}

function renderRareComparables(data) {
  rareUi.result.replaceChildren();
  const head = rareSection('查询条件');
  const facts = rareNode('dl', 'rare-grid');
  const groups = Array.isArray(data.socket_group_sizes)
    ? data.socket_group_sizes.join(' + ') : '未启用';
  facts.append(
    rareField('底材', `${data.base_type_input || '未知'} → ${data.base_type_en || '未知'}`),
    rareField('联盟', data.league || '未选择'),
    rareField('物品等级', data.item_level ?? '未知'),
    rareField('腐化', data.corrupted ? '是' : '否'),
    rareField('品质筛选', data.quality_filter == null ? '未启用' : `${data.quality_filter}%`),
    rareField('连接组尺寸筛选', groups)
  );
  head.append(facts);
  rareUi.result.append(head, rareMapping(data), rareQuote(data));
  const audit = rareAudit(data);
  if (audit) rareUi.result.append(audit);
  rareUi.result.append(rareNode('p', 'rare-boundary', data.reason || '请结合样本范围核对结果。'));
  const url = rareSafeTradeUrl(data.source_url);
  if (url) {
    const link = rareNode('a', 'rare-source', '打开 GGG Trade 原始搜索');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    rareUi.result.append(link);
  }
  rareUi.result.hidden = false;
}

async function rareError(response) {
  try {
    const payload = await response.json();
    const detail = payload && payload.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail.reason === 'string') return detail.reason;
  } catch (_) { /* 非 JSON 错误由状态码说明。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

function rareBusy(value) {
  rareUi.submit.disabled = value;
  rareUi.text.readOnly = value;
  rareUi.league.readOnly = value;
  rareUi.quality.disabled = value;
  rareUi.sockets.disabled = value;
}

async function submitRareComparables(event) {
  event.preventDefault();
  if (rareUi.submit.disabled) return;
  const text = rareUi.text.value.trim();
  if (!text) return;
  const epoch = ++rareEpoch;
  rareUi.result.hidden = true;
  rareUi.result.replaceChildren();
  rareUi.status.textContent = '正在查询 GGG Trade 在线挂单…';
  rareBusy(true);
  const request = {
    item_text: text,
    league: rareUi.league.value.trim() || null,
    match_quality: rareUi.quality.checked,
    match_sockets: rareUi.sockets.checked,
  };
  try {
    const response = await apiFetch('/economy/rare-comparables', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    if (epoch !== rareEpoch || !rareUi.dialog.open) return;
    if (!response.ok) {
      const message = await rareError(response);
      if (epoch === rareEpoch && rareUi.dialog.open) rareUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== rareEpoch || !rareUi.dialog.open) return;
    renderRareComparables(data);
    rareUi.status.textContent = data.available ? '查询完成。请核对样本边界。' : '查询完成，当前没有可用代表价。';
  } catch (error) {
    if (epoch === rareEpoch && rareUi.dialog.open) {
      rareUi.status.textContent = `查询失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === rareEpoch) rareBusy(false);
  }
}

function openRareComparables(itemText = '') {
  rareEpoch += 1;
  rareBusy(false);
  rareUi.result.hidden = true;
  rareUi.result.replaceChildren();
  rareUi.status.textContent = '';
  if (itemText) rareUi.text.value = itemText;
  if (!rareUi.dialog.open) rareUi.dialog.showModal();
  rareUi.text.focus();
}

function clearRareComparables() {
  rareEpoch += 1;
  rareBusy(false);
  rareUi.form.reset();
  rareUi.text.value = '';
  rareUi.result.hidden = true;
  rareUi.result.replaceChildren();
  rareUi.status.textContent = '';
  if (rareUi.dialog.open) rareUi.dialog.close();
}

window.openRareComparables = openRareComparables;
window.clearRareComparables = clearRareComparables;
rareUi.open.addEventListener('click', () => openRareComparables());
rareUi.close.addEventListener('click', () => rareUi.dialog.close());
rareUi.dialog.addEventListener('close', () => {
  if (!rareUi.dialog.open) { rareEpoch += 1; rareBusy(false); }
});
rareUi.form.addEventListener('submit', submitRareComparables);
