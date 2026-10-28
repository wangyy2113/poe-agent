/* 经济分析面板：显式提交已有 HTTP 接口，只渲染结构化证据。 */
'use strict';

const economyUi = {
  open: document.getElementById('economy-open'),
  dialog: document.getElementById('economy-dialog'),
  close: document.getElementById('economy-close'),
  form: document.getElementById('economy-form'),
  mode: document.getElementById('economy-mode'),
  queryFields: document.getElementById('economy-query-fields'),
  query: document.getElementById('economy-query'),
  category: document.getElementById('economy-category'),
  league: document.getElementById('economy-league'),
  trendFields: document.getElementById('economy-trend-fields'),
  window: document.getElementById('economy-window'),
  materialFields: document.getElementById('economy-material-fields'),
  materialRows: document.getElementById('economy-material-rows'),
  addMaterial: document.getElementById('economy-add-material'),
  flipFields: document.getElementById('economy-flip-fields'),
  submit: document.getElementById('economy-submit'),
  status: document.getElementById('economy-status'),
  result: document.getElementById('economy-result'),
};
let economyEpoch = 0;

function economyNode(tag, className = '', value = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = String(value);
  return node;
}

function economySection(title) {
  const section = economyNode('section', 'economy-section');
  section.append(economyNode('h3', '', title));
  return section;
}

function economyField(label, value) {
  const row = economyNode('div', 'economy-field');
  row.append(economyNode('dt', '', label), economyNode('dd', '', value));
  return row;
}

function economyGrid(fields) {
  const grid = economyNode('dl', 'economy-grid');
  for (const [label, value] of fields) grid.append(economyField(label, value));
  return grid;
}

function economyNumber(value, suffix = '') {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(value)}${suffix}`
    : '未提供';
}

function economyTradeLink(raw) {
  try {
    const url = new URL(String(raw));
    if (url.protocol === 'https:' && !url.username && !url.password && !url.port
        && (url.hostname === 'poe.watch'
            || (url.hostname === 'www.pathofexile.com' && url.pathname.startsWith('/trade')))) {
      return url.href;
    }
  } catch (_) { /* 无可用来源链接时展示来源文字。 */ }
  return '';
}

function economySource(section, data) {
  const linkUrl = economyTradeLink(data.source_url);
  const fields = [
    ['联盟', data.league || '未提供'],
    ['来源', data.source || '未提供'],
    ['抓取时间', data.fetched_at || '未提供'],
  ];
  for (const [label, key] of [
    ['缓存', 'from_cache'], ['陈旧', 'stale'],
    ['降级', 'degraded'], ['备用源', 'fallback'],
  ]) {
    if (typeof data[key] === 'boolean') fields.push([label, data[key] ? '是' : '否']);
  }
  section.append(economyGrid(fields));
  if (data.warning) section.append(economyNode('p', 'economy-warning', data.warning));
  if (data.fallback_reason) section.append(economyNode('p', 'economy-muted', data.fallback_reason));
  if (linkUrl) {
    const link = economyNode('a', 'economy-source', '查看来源');
    link.href = linkUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    section.append(link);
  }
}

function renderEconomyPrice(data) {
  const section = economySection('当前价格');
  const prices = Array.isArray(data.prices) ? data.prices : [];
  if (!prices.length) section.append(economyNode('p', 'economy-warning', '没有精确匹配的条目或可用价格。'));
  for (const price of prices) {
    const card = economyNode('article', 'economy-card');
    card.append(economyNode('h4', '', `${price.name || '未知条目'} · ${price.category || '未知类别'}`));
    card.append(economyNode('strong', 'economy-value',
      price.representative_chaos == null ? '无可用代表价'
        : `${economyNumber(price.representative_chaos)} 混沌石`));
    card.append(economyGrid([
      ['价格口径', price.price_basis || '未提供'],
      ['均价', economyNumber(price.mean_chaos, ' 混沌石')],
      ['中位数', economyNumber(price.median_chaos, ' 混沌石')],
      ['样本范围', `${economyNumber(price.min_chaos)}–${economyNumber(price.max_chaos)} 混沌石`],
      ['低置信度', price.low_confidence ? '是' : '否'],
      ['日交易量', economyNumber(price.daily)],
    ]));
    if (price.sample_count != null) {
      card.append(economyGrid([
        ['有效挂单样本', `${price.sample_count} / 至少 ${price.minimum_sample_count ?? '未提供'}`],
        ['搜索命中 / 抓取', `${price.search_total ?? '未提供'} / ${price.fetched_listing_count ?? '未提供'}`],
        ['跨联盟 / 无效报价', `${price.cross_league_filtered_count ?? 0} / ${price.invalid_quote_filtered_count ?? 0}`],
        ['无法换算 / 离群过滤', `${price.unconverted_currency_filtered_count ?? 0} / ${price.outlier_filtered_count ?? 0}`],
        ['最早 / 最新挂单', `${price.earliest_listing_at || '未提供'} / ${price.latest_listing_at || '未提供'}`],
      ]));
    }
    if (price.sample_scope) card.append(economyNode('p', 'economy-muted', price.sample_scope));
    section.append(card);
  }
  return section;
}

function renderEconomyTrend(data) {
  const section = economySection(`历史趋势 · ${data.window_hours ?? '未知'} 小时`);
  const trends = Array.isArray(data.trends) ? data.trends : [];
  if (!trends.length) section.append(economyNode('p', 'economy-warning', '没有精确匹配的历史序列。'));
  for (const trend of trends) {
    const card = economyNode('article', 'economy-card');
    card.append(economyNode('h4', '', trend.name || '未知条目'));
    const ready = trend.status === 'ready';
    card.append(economyNode('strong', 'economy-value', ready
      ? `${economyNumber(trend.change_chaos)} 混沌石 / ${economyNumber(trend.change_percent, '%')}`
      : '观测不足，不判断趋势'));
    card.append(economyGrid([
      ['序列口径', trend.basis || '未提供'],
      ['观测数', `${trend.observation_count ?? 0} / 至少 ${data.minimum_observations ?? 2}`],
      ['逐点时间戳', trend.timestamps_available ? '有' : '无'],
      ['首个 / 最后观测', `${trend.first_observed_at || '未提供'} / ${trend.last_observed_at || '未提供'}`],
      ['首尾均价', ready
        ? `${economyNumber(trend.first_mean_chaos)} → ${economyNumber(trend.last_mean_chaos)} 混沌石`
        : '不输出'],
      ['窗口内均价范围', ready
        ? `${economyNumber(trend.min_mean_chaos)}–${economyNumber(trend.max_mean_chaos)} 混沌石`
        : '不输出'],
      ['低置信度观测', trend.low_confidence_count ?? 0],
    ]));
    if (trend.reason) card.append(economyNode('p', 'economy-muted', trend.reason));
    section.append(card);
  }
  return section;
}

function renderEconomyMaterials(data) {
  const section = economySection('明确材料清单成本');
  section.append(economyNode('strong', 'economy-value',
    data.calculable && data.total_representative_chaos != null
      ? `${economyNumber(data.total_representative_chaos)} 混沌石`
      : '清单有未解析项，不输出部分总价'));
  section.append(economyGrid([
    ['未解析项数', data.unresolved_count ?? '未提供'],
    ['均价合计', data.calculable ? economyNumber(data.total_mean_chaos, ' 混沌石') : '不输出'],
    ['范围合计', data.calculable
      ? `${economyNumber(data.total_min_chaos)}–${economyNumber(data.total_max_chaos)} 混沌石`
      : '不输出'],
  ]));
  for (const line of Array.isArray(data.materials) ? data.materials : []) {
    const card = economyNode('article', 'economy-card');
    card.append(economyNode('h4', '', `${line.query || '未知材料'} × ${economyNumber(line.quantity)}`));
    card.append(economyGrid([
      ['匹配', line.resolved_name || line.status || '未解析'],
      ['单价', line.unit_price ? economyNumber(line.unit_price.representative_chaos, ' 混沌石') : '未提供'],
      ['单价口径', line.unit_price?.price_basis || '未提供'],
      ['小计', line.subtotal_representative_chaos == null
        ? '不输出' : economyNumber(line.subtotal_representative_chaos, ' 混沌石')],
      ['低置信度', line.low_confidence ? '是' : '否'],
      ['来源 / 抓取时间', `${line.source || '未提供'} / ${line.fetched_at || '未提供'}`],
    ]));
    if (line.reason) card.append(economyNode('p', 'economy-muted', line.reason));
    section.append(card);
  }
  return section;
}

function economySide(title, side) {
  const section = economySection(title);
  section.append(economyGrid([
    ['单位报价', economyNumber(side?.unit_chaos, ' 混沌石')],
    ['有效样本', `${side?.sample_count ?? 0} / 至少 ${side?.minimum_sample_count ?? '未提供'}`],
    ['原始挂单', side?.raw_listing_count ?? '未提供'],
    ['跨联盟 / 无效报价', `${side?.cross_league_filtered_count ?? 0} / ${side?.invalid_quote_filtered_count ?? 0}`],
    ['库存不足 / 超时', `${side?.insufficient_stock_filtered_count ?? 0} / ${side?.stale_listing_filtered_count ?? 0}`],
    ['离群过滤', side?.outlier_filtered_count ?? 0],
    ['最早 / 最新挂单', `${side?.earliest_listing_at || '未提供'} / ${side?.latest_listing_at || '未提供'}`],
  ]));
  if (side?.sample_scope) section.append(economyNode('p', 'economy-muted', side.sample_scope));
  return section;
}

function renderEconomyFlip(data) {
  const section = economySection('双边价差');
  const ready = data.calculable === true && data.net_profit_chaos != null;
  section.append(economyNode('strong', 'economy-value', ready
    ? `${economyNumber(data.net_profit_chaos)} 混沌石 · ${economyNumber(data.margin_percent, '%')}`
    : '双边证据不足，利润与判断未知'));
  if (ready) section.append(economyNode('p', 'economy-muted',
    data.verdict === 'flip_candidate' ? '当前挂单快照达到调用方阈值的候选' : '当前挂单快照未达到调用方阈值'));
  section.append(economyGrid([
    ['条目 / 数量', `${data.resolved_name || data.query || '未知'} × ${economyNumber(data.quantity)}`],
    ['挂单时限', `${data.max_listing_age_hours ?? '未提供'} 小时`],
    ['最低净利润 / 利润率', `${economyNumber(data.minimum_net_profit_chaos)} 混沌石 / ${economyNumber(data.minimum_margin_percent, '%')}`],
    ['买入总成本', ready ? economyNumber(data.total_buy_cost_chaos, ' 混沌石') : '不输出'],
    ['卖出净收入', ready ? economyNumber(data.net_sell_revenue_chaos, ' 混沌石') : '不输出'],
    ['买入 / 卖出费用', ready
      ? `${economyNumber(data.buy_fee_chaos)} / ${economyNumber(data.sell_fee_chaos)} 混沌石`
      : '不输出'],
  ]));
  if (data.reason) section.append(economyNode('p', 'economy-muted', data.reason));
  const fees = data.fees || {};
  section.append(economyNode('p', 'economy-muted',
    `显式费用：买入固定 ${economyNumber(fees.buy_fixed_chaos)} 混沌石 + ${economyNumber(fees.buy_percent, '%')}；` +
    `卖出固定 ${economyNumber(fees.sell_fixed_chaos)} 混沌石 + ${economyNumber(fees.sell_percent, '%')}。`));
  economyUi.result.append(section, economySide('买入侧挂单', data.buy_side), economySide('卖出侧挂单', data.sell_side));
}

function renderEconomy(data, mode) {
  economyUi.result.replaceChildren();
  const source = economySection('来源与时效');
  economySource(source, data);
  economyUi.result.append(source);
  if (mode === 'price') economyUi.result.append(renderEconomyPrice(data));
  if (mode === 'trend') economyUi.result.append(renderEconomyTrend(data));
  if (mode === 'materials') economyUi.result.append(renderEconomyMaterials(data));
  if (mode === 'flip') renderEconomyFlip(data);
  economyUi.result.append(economyNode('p', 'economy-boundary',
    '价格快照、在线挂单和历史序列均不保证成交；材料成本不推断制作路径，双边候选不保证收益。'));
  economyUi.result.hidden = false;
}

function economyBusy(value) { economyUi.submit.disabled = value; }

function economyInvalidate() {
  economyEpoch += 1;
  economyBusy(false);
  economyUi.status.textContent = '';
  economyUi.result.hidden = true;
  economyUi.result.replaceChildren();
}

function economyModeChanged() {
  economyInvalidate();
  const mode = economyUi.mode.value;
  economyUi.queryFields.hidden = mode === 'materials';
  economyUi.trendFields.hidden = mode !== 'trend';
  economyUi.materialFields.hidden = mode !== 'materials';
  economyUi.flipFields.hidden = mode !== 'flip';
  economyUi.submit.textContent = {
    price: '查询当前价格', trend: '查询历史趋势',
    materials: '计算清单成本', flip: '核验双边价差',
  }[mode] || '查询';
}

function economyAddMaterial() {
  if (economyUi.materialRows.children.length >= 50) return;
  const row = economyNode('div', 'economy-material-row');
  const name = economyNode('input');
  name.placeholder = '材料名称';
  name.maxLength = 200;
  name.setAttribute('aria-label', '材料名称');
  const quantity = economyNode('input');
  quantity.type = 'number';
  quantity.min = '0';
  quantity.max = '1000000000';
  quantity.step = 'any';
  quantity.value = '1';
  quantity.setAttribute('aria-label', '材料数量');
  const category = economyNode('select');
  category.setAttribute('aria-label', '材料类别');
  for (const [value, label] of [
    ['auto', '自动识别'], ['unique', '唯一装备'], ['currency', '通货'],
    ['scarab', '圣甲虫'], ['card', '命运卡'], ['fragment', '碎片'],
  ]) {
    const option = economyNode('option', '', label);
    option.value = value;
    category.append(option);
  }
  category.value = 'auto';
  const remove = economyNode('button', 'ghost', '移除');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    if (economyUi.materialRows.children.length > 1) { row.remove(); economyInvalidate(); }
  });
  row.append(name, quantity, category, remove);
  row.nameInput = name;
  row.quantityInput = quantity;
  row.categorySelect = category;
  economyUi.materialRows.append(row);
  economyInvalidate();
}

function economyRequiredNumber(raw, label, min, max, integer = false, strictMin = false, strictMax = false) {
  const value = String(raw).trim() === '' ? NaN : Number(raw);
  if (!Number.isFinite(value) || (strictMin ? value <= min : value < min)
      || (strictMax ? value >= max : value > max) || (integer && !Number.isInteger(value))) {
    throw new Error(`${label}必须是${strictMin ? '大于' : '不小于'} ${min}、` +
      `${strictMax ? '小于' : '不大于'} ${max} 的${integer ? '整数' : '有限数'}`);
  }
  return value;
}

function economyRequest() {
  const mode = economyUi.mode.value;
  const league = economyUi.league.value.trim() || null;
  if (mode === 'materials') {
    const materials = Array.from(economyUi.materialRows.children, (row, index) => {
      const query = row.nameInput.value.trim();
      if (!query) throw new Error(`第 ${index + 1} 项材料名称不能为空`);
      return {
        query,
        quantity: economyRequiredNumber(row.quantityInput.value, `第 ${index + 1} 项数量`, 0, 1_000_000_000, false, true),
        category: row.categorySelect.value,
      };
    });
    if (!materials.length) throw new Error('至少输入一项材料');
    return { path: '/economy/material-cost', body: { materials, league } };
  }
  const query = economyUi.query.value.trim();
  if (!query) throw new Error('物品名称不能为空');
  const category = economyUi.category.value;
  if (mode === 'price') return { path: '/economy/price', body: { query, category, league } };
  if (mode === 'trend') return {
    path: '/economy/trend',
    body: { query, category, league,
      window_hours: economyRequiredNumber(economyUi.window.value, '观察窗口', 1, 2160, true) },
  };
  if (mode !== 'flip') throw new Error('未知分析类型');
  if (category === 'unique') throw new Error('唯一装备没有可验证的 exchange 双边深度');
  const get = (id, label, min, max, integer = false, strictMin = false, strictMax = false) => economyRequiredNumber(
    document.getElementById(id).value, label, min, max, integer, strictMin, strictMax
  );
  return {
    path: '/economy/flip-analysis',
    body: {
      query, category, league,
      quantity: get('economy-quantity', '交易数量', 0, 1_000_000_000, false, true),
      max_listing_age_hours: get('economy-age', '挂单时限', 1, 168, true),
      fees: {
        buy_fixed_chaos: get('economy-buy-fixed', '买入固定费用', 0, 1_000_000_000),
        buy_percent: get('economy-buy-percent', '买入比例费用', 0, 100, false, false, true),
        sell_fixed_chaos: get('economy-sell-fixed', '卖出固定费用', 0, 1_000_000_000),
        sell_percent: get('economy-sell-percent', '卖出比例费用', 0, 100, false, false, true),
      },
      minimum_net_profit_chaos: get('economy-min-profit', '最低净利润', 0, Number.MAX_VALUE),
      minimum_margin_percent: get('economy-min-margin', '最低利润率', 0, Number.MAX_VALUE),
    },
  };
}

async function economyError(response) {
  try {
    const payload = await response.json();
    const detail = payload?.detail;
    if (typeof detail === 'string') return detail;
    if (typeof detail?.reason === 'string') return detail.reason;
  } catch (_) { /* 非 JSON 错误以 HTTP 状态说明。 */ }
  return `请求失败（HTTP ${response.status}）`;
}

async function submitEconomy(event) {
  event.preventDefault();
  if (economyUi.submit.disabled) return;
  let request;
  try { request = economyRequest(); }
  catch (error) { economyUi.status.textContent = String(error.message || error); return; }
  const mode = economyUi.mode.value;
  const epoch = ++economyEpoch;
  economyUi.result.hidden = true;
  economyUi.result.replaceChildren();
  economyUi.status.textContent = '正在查询…';
  economyBusy(true);
  try {
    const response = await apiFetch(request.path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request.body),
    });
    if (epoch !== economyEpoch || !economyUi.dialog.open) return;
    if (!response.ok) {
      const message = await economyError(response);
      if (epoch === economyEpoch && economyUi.dialog.open) economyUi.status.textContent = message;
      return;
    }
    const data = await response.json();
    if (epoch !== economyEpoch || !economyUi.dialog.open) return;
    renderEconomy(data, mode);
    economyUi.status.textContent = '查询完成。请核对来源、样本与适用边界。';
  } catch (error) {
    if (epoch === economyEpoch && economyUi.dialog.open) {
      economyUi.status.textContent = `查询失败：${String(error.message || error)}`;
    }
  } finally {
    if (epoch === economyEpoch) economyBusy(false);
  }
}

function openEconomyWorkbench() {
  economyInvalidate();
  if (!economyUi.dialog.open) economyUi.dialog.showModal();
  (economyUi.mode.value === 'materials' ? economyUi.mode : economyUi.query).focus();
}

function clearEconomyWorkbench() {
  economyInvalidate();
  economyUi.form.reset();
  economyUi.query.value = '';
  economyUi.league.value = '';
  economyUi.materialRows.replaceChildren();
  economyAddMaterial();
  economyModeChanged();
  if (economyUi.dialog.open) economyUi.dialog.close();
}

window.clearEconomyWorkbench = clearEconomyWorkbench;
economyUi.open.addEventListener('click', openEconomyWorkbench);
economyUi.close.addEventListener('click', () => economyUi.dialog.close());
economyUi.dialog.addEventListener('close', () => {
  if (!economyUi.dialog.open) economyInvalidate();
});
economyUi.mode.addEventListener('change', economyModeChanged);
economyUi.form.addEventListener('input', economyInvalidate);
economyUi.form.addEventListener('change', economyInvalidate);
economyUi.form.addEventListener('submit', submitEconomy);
economyUi.addMaterial.addEventListener('click', economyAddMaterial);
economyAddMaterial();
economyModeChanged();
