/* POE1-Agent 前端。原生 JS，无框架、无构建、无 CDN。
 *
 * 两个设计点值得说明：
 *
 * 1. 流式粒度是**句**不是字。后端的出口闸门必须等一句说完才能判断它是不是事实
 *    断言、绑定的编号有效不有效、句中数值能不能溯源。逐字推送等于把未校验内容
 *    先给了用户，之后没法撤回。这里对已通过的句子做打字机效果，观感接近逐字流，
 *    但显示的每个字都已过校验。
 *
 * 2. Markdown 自己渲染，不引 marked。答案的 Markdown 形态有限（标题、粗体、
 *    列表、表格、代码、分隔线），自己写百来行够用；更重要的是能保证**先转义
 *    HTML 再解析**，知识库内容或模型输出里的 <script> 不可能被执行。
 */
'use strict';

/* 接口前缀从当前页面路径推出，不硬编码 /poe_agent —— 前缀是
 * config.yaml 的 server.base_path，改了配置前端不该跟着改。
 * 去掉最后一段路径：/poe_agent/ 与 /poe_agent/index.html 都得到 /poe_agent。 */
const API = location.pathname.replace(/\/[^/]*$/, '') || '';
const $ = (id) => document.getElementById(id);

const el = {
  main: $('main'), thread: $('thread'), empty: $('empty'),
  input: $('input'), send: $('send'), pasteCard: $('paste-card'),
  newChat: $('new-chat'), renameChat: $('rename-chat'), deleteChat: $('delete-chat'),
  characterImport: $('import-character'), convs: $('conversations'),
  imageUpload: $('upload-image'), imageInput: $('image-input'),
  authKey: $('auth-key'), authDialog: $('auth-dialog'), authForm: $('auth-form'),
  authInput: $('auth-input'), authError: $('auth-error'),
  authClear: $('auth-clear'), authCancel: $('auth-cancel'), authSave: $('auth-save'),
  manageMemories: $('manage-memories'), memoryDialog: $('memory-dialog'),
  memoryClose: $('memory-close'), memoryStatus: $('memory-status'),
  memoryList: $('memory-list'), memoryForm: $('memory-form'),
  memoryFormTitle: $('memory-form-title'), memoryCategory: $('memory-category'),
  memoryLabel: $('memory-label'), memoryContent: $('memory-content'),
  memoryActive: $('memory-active'), memoryError: $('memory-error'),
  memoryReset: $('memory-reset'), memorySave: $('memory-save'),
};

let conversationId = null;
let busy = false;
let authRequired = false;

/* ── API 鉴权 ────────────────────────────────────────── */

const AUTH_STORE = 'poe_agent_api_key';
const CONVERSATION_STORE = 'poe_agent_convs';

function storedApiKey() {
  return sessionStorage.getItem(AUTH_STORE) || '';
}

function updateAuthButton(required = false) {
  const configured = Boolean(storedApiKey());
  el.authKey.textContent = configured ? 'API 密钥 ✓' : (required ? '配置 API 密钥' : 'API 密钥');
  el.authKey.classList.toggle('auth-required', required && !configured);
}

function showAuthDialog(message = '') {
  el.authError.textContent = message;
  el.authError.hidden = !message;
  el.authInput.value = '';
  if (!el.authDialog.open) el.authDialog.showModal();
  el.authInput.focus();
}

async function requestApi(path, options = {}, explicitKey) {
  const headers = new Headers(options.headers || {});
  const key = explicitKey === undefined ? storedApiKey() : explicitKey;
  if (key) headers.set('Authorization', `Bearer ${key}`);
  return fetch(`${API}${path}`, { ...options, headers });
}

async function apiFetch(path, options = {}) {
  const hadKey = Boolean(storedApiKey());
  const response = await requestApi(path, options);
  if (response.status === 401) {
    authRequired = true;
    if (hadKey) {
      sessionStorage.removeItem(AUTH_STORE);
      clearConversationUi();
      clearMemoryUi();
      window.clearRareComparables?.();
    }
    updateAuthButton(true);
    showAuthDialog(hadKey ? 'API Key 无效或已失效，请重新输入。' : '此服务需要 API Key。');
  }
  return response;
}

function clearConversationUi() {
  conversationId = null;
  sessionStorage.removeItem(CONVERSATION_STORE);
  localStorage.removeItem(CONVERSATION_STORE);
  el.thread.innerHTML = '';
  el.empty.style.display = '';
  renderConversationList();
}

async function saveApiKey(event) {
  event.preventDefault();
  const key = el.authInput.value.trim();
  if (!key) {
    el.authError.textContent = 'API Key 不能为空。';
    el.authError.hidden = false;
    return;
  }
  el.authSave.disabled = true;
  el.authError.hidden = true;
  try {
    const response = await requestApi('/conversations?limit=1', {}, key);
    if (response.status === 401) {
      el.authError.textContent = 'API Key 无效。';
      el.authError.hidden = false;
      return;
    }
    if (!response.ok) {
      el.authError.textContent = `验证失败（HTTP ${response.status}）。`;
      el.authError.hidden = false;
      return;
    }
    const previous = storedApiKey();
    if (previous !== key) {
      clearConversationUi();
      clearMemoryUi();
      window.clearRareComparables?.();
    }
    sessionStorage.setItem(AUTH_STORE, key);
    authRequired = false;
    el.authInput.value = '';
    el.authDialog.close();
    updateAuthButton(false);
    await syncConversations();
  } catch (err) {
    el.authError.textContent = `连接失败：${String(err.message || err)}`;
    el.authError.hidden = false;
  } finally {
    el.authSave.disabled = false;
  }
}

function clearApiKey() {
  sessionStorage.removeItem(AUTH_STORE);
  el.authInput.value = '';
  el.authDialog.close();
  clearConversationUi();
  clearMemoryUi();
  window.clearRareComparables?.();
  updateAuthButton(authRequired);
}

/* ── 显式长期记忆 ────────────────────────────────────── */

const MEMORY_CATEGORY_LABELS = Object.freeze({
  preference: '偏好', goal: '目标', constraint: '约束', note: '备注',
});
let memoryItems = [];
let editingMemoryId = null;
let memoryBusy = false;
let memoryEpoch = 0;

function resetMemoryForm() {
  editingMemoryId = null;
  el.memoryFormTitle.textContent = '新增记忆';
  el.memoryCategory.value = 'preference';
  el.memoryLabel.value = '';
  el.memoryContent.value = '';
  el.memoryActive.checked = true;
  el.memoryError.textContent = '';
  el.memoryError.hidden = true;
  el.memorySave.textContent = '保存记忆';
}

function clearMemoryUi() {
  memoryEpoch += 1;
  memoryItems = [];
  editingMemoryId = null;
  el.memoryList.replaceChildren();
  el.memoryStatus.textContent = '';
  resetMemoryForm();
  setMemoryBusy(false);
  if (el.memoryDialog.open) el.memoryDialog.close();
}

function setMemoryBusy(value) {
  memoryBusy = value;
  el.memoryForm.querySelectorAll('input, select, textarea, button').forEach((control) => {
    control.disabled = value;
  });
  el.memoryList.querySelectorAll('button').forEach((button) => {
    button.disabled = value;
  });
}

function setMemoryError(message) {
  el.memoryError.textContent = message;
  el.memoryError.hidden = !message;
}

function memoryApiError(data, fallback) {
  return data && typeof data.detail === 'string' ? data.detail : fallback;
}

function memoryActionButton(action, id, text) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'ghost';
  button.dataset.memoryAction = action;
  button.dataset.memoryId = id;
  button.textContent = text;
  return button;
}

function renderMemories() {
  el.memoryList.replaceChildren();
  if (!memoryItems.length) {
    const empty = document.createElement('div');
    empty.className = 'memory-empty';
    empty.textContent = '还没有长期记忆。只会保存你在这里主动提交的内容。';
    el.memoryList.appendChild(empty);
    return;
  }
  for (const item of memoryItems) {
    const card = document.createElement('article');
    card.className = `memory-item${item.active ? '' : ' inactive'}`;

    const head = document.createElement('div');
    head.className = 'memory-item-head';
    const category = document.createElement('span');
    category.className = 'memory-category';
    category.textContent = MEMORY_CATEGORY_LABELS[item.category] || item.category;
    const label = document.createElement('span');
    label.className = 'memory-item-label';
    label.textContent = item.label || '未命名';
    head.append(category, label);

    const content = document.createElement('div');
    content.className = 'memory-item-content';
    content.textContent = item.content;

    const actions = document.createElement('div');
    actions.className = 'memory-item-actions';
    actions.append(
      memoryActionButton('edit', item.memory_id, '编辑'),
      memoryActionButton('toggle', item.memory_id, item.active ? '停用' : '启用'),
      memoryActionButton('delete', item.memory_id, '删除'),
    );
    card.append(head, content, actions);
    el.memoryList.appendChild(card);
  }
}

async function loadMemories(epoch = memoryEpoch) {
  el.memoryStatus.textContent = '正在读取…';
  setMemoryBusy(true);
  try {
    const response = await apiFetch('/memories?include_inactive=true&limit=100');
    const data = await response.json().catch(() => null);
    if (epoch !== memoryEpoch || !el.memoryDialog.open) return;
    if (!response.ok) {
      if (response.status !== 401) {
        el.memoryStatus.textContent = memoryApiError(data, `读取失败（HTTP ${response.status}）`);
      }
      return;
    }
    memoryItems = Array.isArray(data.items) ? data.items : [];
    renderMemories();
    const activeCount = memoryItems.filter((item) => item.active).length;
    el.memoryStatus.textContent = `共 ${data.total} 条，已启用 ${activeCount} 条`;
  } catch (err) {
    if (epoch === memoryEpoch && el.memoryDialog.open) {
      el.memoryStatus.textContent = `读取失败：${String(err.message || err)}`;
    }
  } finally {
    if (epoch === memoryEpoch) setMemoryBusy(false);
  }
}

async function showMemoryDialog() {
  const epoch = ++memoryEpoch;
  resetMemoryForm();
  if (!el.memoryDialog.open) el.memoryDialog.showModal();
  await loadMemories(epoch);
}

async function saveMemory(event) {
  event.preventDefault();
  if (memoryBusy) return;
  const content = el.memoryContent.value.trim();
  if (!content) {
    setMemoryError('记忆内容不能为空。');
    return;
  }
  const payload = {
    category: el.memoryCategory.value,
    label: el.memoryLabel.value.trim(),
    content,
    active: el.memoryActive.checked,
  };
  const memoryId = editingMemoryId;
  const epoch = memoryEpoch;
  let saved = false;
  setMemoryError('');
  setMemoryBusy(true);
  try {
    const response = await apiFetch(
      memoryId ? `/memories/${encodeURIComponent(memoryId)}` : '/memories',
      {
        method: memoryId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    const data = await response.json().catch(() => null);
    if (epoch !== memoryEpoch) return;
    if (!response.ok) {
      if (response.status !== 401) {
        setMemoryError(memoryApiError(data, `保存失败（HTTP ${response.status}）`));
      }
      return;
    }
    saved = true;
  } catch (err) {
    if (epoch === memoryEpoch) setMemoryError(`保存失败：${String(err.message || err)}`);
  } finally {
    if (epoch === memoryEpoch) setMemoryBusy(false);
  }
  if (saved) {
    resetMemoryForm();
    await loadMemories(epoch);
  }
}

function editMemory(memoryId) {
  const item = memoryItems.find((candidate) => candidate.memory_id === memoryId);
  if (!item) return;
  editingMemoryId = memoryId;
  el.memoryFormTitle.textContent = '编辑记忆';
  el.memoryCategory.value = item.category;
  el.memoryLabel.value = item.label;
  el.memoryContent.value = item.content;
  el.memoryActive.checked = item.active;
  el.memorySave.textContent = '保存修改';
  setMemoryError('');
  el.memoryContent.focus();
}

async function mutateMemory(memoryId, action) {
  if (memoryBusy) return;
  const item = memoryItems.find((candidate) => candidate.memory_id === memoryId);
  if (!item) return;
  if (action === 'delete' && !window.confirm('永久删除这条长期记忆？')) return;
  const deleting = action === 'delete';
  const epoch = memoryEpoch;
  let changed = false;
  setMemoryBusy(true);
  try {
    const response = await apiFetch(`/memories/${encodeURIComponent(memoryId)}`, {
      method: deleting ? 'DELETE' : 'PATCH',
      headers: deleting ? {} : { 'Content-Type': 'application/json' },
      body: deleting ? undefined : JSON.stringify({ active: !item.active }),
    });
    const data = await response.json().catch(() => null);
    if (epoch !== memoryEpoch) return;
    if (!response.ok) {
      if (response.status !== 401) {
        el.memoryStatus.textContent = memoryApiError(
          data, `${deleting ? '删除' : '更新'}失败（HTTP ${response.status}）`,
        );
      }
      return;
    }
    changed = true;
  } catch (err) {
    if (epoch === memoryEpoch) {
      el.memoryStatus.textContent = `${deleting ? '删除' : '更新'}失败：${String(err.message || err)}`;
    }
  } finally {
    if (epoch === memoryEpoch) setMemoryBusy(false);
  }
  if (changed) {
    if (editingMemoryId === memoryId) resetMemoryForm();
    await loadMemories(epoch);
  }
}

/* ── Markdown ─────────────────────────────────────────── */

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** 行内元素。输入必须**已转义**。 */
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    // 需求 6.6：引用可点击跳转。角标由 [[E1]] / [[E1,E2]] 渲染而来
    .replace(/\[\[\s*((?:E\d+\s*[,，;；]?\s*)+)\]\]/gi, (_, group) => {
      const labels = (group.match(/E\d+/gi) || []).map((x) => x.toUpperCase());
      return labels.map((lb) =>
        `<sup class="cite" data-label="${lb}">${lb.slice(1)}</sup>`).join('');
    })
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/&lt;(https?:\/\/[^\s&]+)&gt;/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

/** 块级渲染。支持标题 / 列表 / 表格 / 代码块 / 引用 / 分隔线 / 段落。 */
function markdown(src) {
  const lines = escapeHtml(src).split('\n');
  const out = [];
  let i = 0;

  const isTableRow = (s) => /^\|.*\|\s*$/.test(s.trim());
  const cells = (s) => s.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());

  while (i < lines.length) {
    const line = lines[i];
    const t = line.trim();

    if (!t) { i++; continue; }

    // 代码块
    if (/^```/.test(t)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${buf.join('\n')}</code></pre>`);
      continue;
    }
    // 分隔线
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { out.push('<hr>'); i++; continue; }
    // 标题
    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lv = Math.min(h[1].length, 4);
      out.push(`<h${lv}>${inline(h[2])}</h${lv}>`);
      i++;
      continue;
    }
    // 引用块
    if (/^&gt;\s?/.test(t)) {
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*&gt;\s?/, ''));
        i++;
      }
      out.push(`<blockquote>${inline(buf.join(' '))}</blockquote>`);
      continue;
    }
    // 表格：表头 + 分隔行 + 数据行
    if (isTableRow(t) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1].trim())) {
      const head = cells(lines[i]); i += 2;
      const rows = [];
      while (i < lines.length && isTableRow(lines[i])) rows.push(cells(lines[i++]));
      out.push(
        '<table><thead><tr>' + head.map((c) => `<th>${inline(c)}</th>`).join('') +
        '</tr></thead><tbody>' +
        rows.map((r) => '<tr>' + r.map((c) => `<td>${inline(c)}</td>`).join('') + '</tr>').join('') +
        '</tbody></table>'
      );
      continue;
    }
    // 列表
    const li = t.match(/^([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      /* 列表按**缩进**分层。原先直接 trim 掉缩进，于是子项被拉平成兄弟项——
       * `/item` 的装备报告正是「每条词缀一项、档位明细缩进一层」，拉平后
       * 明细看起来像独立词缀。 */
      const block = [];
      while (i < lines.length) {
        const raw = lines[i];
        const m = raw.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m) break;
        block.push({ indent: m[1].length, ordered: /\d/.test(m[2]), text: m[3] });
        i++;
      }
      out.push(renderList(block, 0));
      continue;
    }
    // 段落：连续非空行合并
    const buf = [];
    while (i < lines.length && lines[i].trim() &&
           !/^(#{1,6}\s|```|&gt;|[-*+]\s|\d+[.)]\s|\|)/.test(lines[i].trim()) &&
           !/^(-{3,}|\*{3,}|_{3,})$/.test(lines[i].trim())) {
      buf.push(lines[i].trim());
      i++;
    }
    if (buf.length) out.push(`<p>${inline(buf.join(' '))}</p>`);
    else i++;
  }
  return out.join('');
}

/** 把带缩进的列表项渲染成嵌套 ul/ol。`level` 是当前层的缩进量。 */
function renderList(items, start) {
  if (!items.length) return '';
  /* 基准缩进取整块的最小值，不取首项的。首项恰好缩进更深时（手写 Markdown
   * 里常见），按首项算会让后面缩进更浅的项落在循环条件外被静默丢掉。 */
  const baseIndent = Math.min(...items.slice(start).map((x) => x.indent));
  const tag = items[start].ordered ? 'ol' : 'ul';
  const parts = [];
  let i = start;
  while (i < items.length && items[i].indent >= baseIndent) {
    if (items[i].indent > baseIndent) {
      // 子层：整段交给递归，挂进上一个 li 里
      const childStart = i;
      while (i < items.length && items[i].indent > baseIndent) i++;
      const child = renderList(items.slice(childStart, i), 0);
      if (parts.length) parts[parts.length - 1] += child;
      else parts.push(`<li>${child}</li>`);
      continue;
    }
    parts.push(`<li>${inline(items[i].text)}`);
    i++;
    // li 的闭合推迟到确认没有子层之后，所以这里先不闭合
  }
  return `<${tag}>` + parts.map((p) => `${p}</li>`).join('') + `</${tag}>`;
}

/* ── 消息渲染 ─────────────────────────────────────────── */

function addUserMessage(text) {
  el.empty.style.display = 'none';
  const wrap = document.createElement('div');
  wrap.className = 'msg user';
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = text;
  wrap.appendChild(body);
  el.thread.appendChild(wrap);
  scrollToEnd();
}

/** 助手消息容器。返回一组操作它的方法。 */
function addAssistantMessage() {
  const wrap = document.createElement('div');
  wrap.className = 'msg assistant';
  const body = document.createElement('div');
  body.className = 'body';
  const status = document.createElement('div');
  status.className = 'status';
  status.innerHTML = '<span class="dots"><span></span><span></span><span></span></span><span class="label">检索中</span>';
  body.appendChild(status);
  wrap.appendChild(body);
  el.thread.appendChild(wrap);
  scrollToEnd();

  /* 正文按**段**累积：每段是一个 div，整段重渲染。
   * 不逐句 append HTML，因为 Markdown 结构（列表、表格）会跨句，
   * 逐句拼 HTML 会把它们拆断。
   *
   * 遇到拦截或缺据时**封段**：下一句开新段。这样拦截提示出现在它实际发生的
   * 位置，而不是全堆到末尾——堆到末尾用户就看不出是哪里被拦了。 */
  let markdownBuffer = '';
  let prose = null;

  const ensureProse = () => {
    if (!prose) {
      prose = document.createElement('div');
      markdownBuffer = '';
      body.insertBefore(prose, status);
    }
    return prose;
  };

  /** 封掉当前正文段，下一句会开新段。 */
  const sealProse = () => { prose = null; markdownBuffer = ''; };

  return {
    setStatus(label) {
      const l = status.querySelector('.label');
      if (l) l.textContent = label;
    },
    clearStatus() { status.remove(); },

    /** 追加一句已校验通过的正文，带打字机效果。 */
    async appendSentence(text, kind) {
      const target = ensureProse();
      const before = markdownBuffer;
      // 打字机：逐字重渲染。句子已过校验，所以显示的每个字都是安全的。
      const step = text.length > 90 ? 6 : 2;
      for (let n = step; n < text.length; n += step) {
        markdownBuffer = before + text.slice(0, n);
        target.innerHTML = markdown(markdownBuffer);
        scrollToEnd();
        await sleep(8);
      }
      markdownBuffer = before + text;
      target.innerHTML = markdown(markdownBuffer);
      if (kind === 'recommendation') target.classList.add('has-recommendation');
      scrollToEnd();
    },

    /** 被拦截的无据断言。显示的是拦截说明，不是原文。 */
    appendBlocked(text, reason) {
      const d = document.createElement('div');
      d.className = 'blocked';
      d.innerHTML = `<b>已拦截</b>：${escapeHtml(reason)}`;
      body.insertBefore(d, status);
      sealProse();
      scrollToEnd();
    },

    /** 缺据声明。与正文视觉分区（需求 6.8）。 */
    appendGap(text) {
      const d = document.createElement('div');
      d.className = 'evidence-gap';
      d.innerHTML = `<div class="tag">查不到 · 未作答</div>${markdown(text)}`;
      body.insertBefore(d, status);
      sealProse();
      scrollToEnd();
    },

    appendCitations(items) {
      const d = document.createElement('div');
      d.className = 'sources';
      d.innerHTML = '<h4>引用</h4><ol>' + items.map((c) => (
        `<li id="src-${c.label}">` +
        `<a href="${escapeHtml(c.url)}" target="_blank" rel="noopener">${escapeHtml(c.title)}</a>` +
        `<span class="src-tag">${escapeHtml(c.source)} · P${c.priority}</span></li>`
      )).join('') + '</ol>';
      body.insertBefore(d, status);
      scrollToEnd();
    },

    appendNote(text) {
      const d = document.createElement('div');
      d.className = 'note';
      d.textContent = text;
      body.insertBefore(d, status);
    },

    /** 整段 Markdown 一次渲染。给 `/item` 这类非流式、代码生成的报告用。
     *  不做打字机：报告是结构化文本，逐字重渲染会让表格与列表反复重排。 */
    async appendMarkdown(text) {
      const target = ensureProse();
      markdownBuffer = text;
      target.innerHTML = markdown(text);
      sealProse();
      scrollToEnd();
    },

    /** 装备文本解析失败（需求 7.6）：给原因 + 期望格式，不给半个结果。 */
    appendParseFailure(detail) {
      const d = document.createElement('div');
      d.className = 'evidence-gap';
      d.innerHTML =
        '<div class="tag">解析失败 · 未作分析</div>' +
        `<p>${escapeHtml(detail.reason || '无法识别这段文本')}</p>` +
        (detail.hint ? `<p>${escapeHtml(detail.hint)}</p>` : '') +
        (detail.expected_sample
          ? `<p>期望的格式：</p><pre><code>${escapeHtml(detail.expected_sample)}</code></pre>`
          : '');
      body.insertBefore(d, status);
      sealProse();
      scrollToEnd();
    },

    appendError(msg) {
      const d = document.createElement('div');
      d.className = 'blocked';
      d.innerHTML = `<b>出错</b>：${escapeHtml(msg)}`;
      body.insertBefore(d, status);
    },

    finish(info) {
      this.clearStatus();
      if (!body.querySelector('p, li, h1, h2, h3, h4, .evidence-gap, .blocked')) {
        const d = document.createElement('div');
        d.className = 'note';
        d.textContent = '本轮没有产生可下发的内容。';
        body.appendChild(d);
      }
      if (info && info.blocked_claims > 0) {
        const d = document.createElement('div');
        d.className = 'note';
        d.textContent =
          `出口校验拦截了 ${info.blocked_claims} 句无据断言（trace ${info.trace_id.slice(0, 8)}）。`;
        body.appendChild(d);
      }
    },
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function scrollToEnd() {
  // 用户往上翻看历史时不要把他拽回底部
  const nearBottom = el.main.scrollHeight - el.main.scrollTop - el.main.clientHeight < 160;
  if (nearBottom) el.main.scrollTop = el.main.scrollHeight;
}

/* 点击角标滚到对应来源并高亮 */
document.addEventListener('click', (e) => {
  const sup = e.target.closest('sup.cite');
  if (!sup) return;
  const li = document.getElementById(`src-${sup.dataset.label}`);
  if (!li) return;
  li.scrollIntoView({ behavior: 'smooth', block: 'center' });
  li.classList.add('hl');
  setTimeout(() => li.classList.remove('hl'), 1400);
});

/* ── 发送 ─────────────────────────────────────────────── */

async function send(text) {
  if (busy || !text.trim()) return;
  if (authRequired && !storedApiKey()) {
    showAuthDialog('此服务需要 API Key。');
    return;
  }
  busy = true;
  el.send.disabled = true;
  addUserMessage(text);
  el.input.value = '';
  autoGrow();

  const msg = addAssistantMessage();
  try {
    const resp = await apiFetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let carry = '';
    let done = null;

    while (true) {
      const { value, done: finished } = await reader.read();
      if (finished) break;
      carry += decoder.decode(value, { stream: true });
      // SSE 帧以空行分隔
      const frames = carry.split('\n\n');
      carry = frames.pop();
      for (const frame of frames) {
        const line = frame.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
        await handleEvent(ev, msg);
        if (ev.type === 'done') done = ev;
      }
    }
    msg.finish(done);
  } catch (err) {
    msg.appendError(String(err.message || err));
    msg.finish(null);
  } finally {
    busy = false;
    el.send.disabled = false;
    el.input.focus();
    refreshConversations();
  }
}

async function handleEvent(ev, msg) {
  switch (ev.type) {
    case 'meta':
      conversationId = ev.conversation_id || conversationId;
      break;
    case 'thinking':
      // 只显示状态。推理正文永远不下发——那是模型凭记忆猜测的地方，
      // 透出去等于绕过全部校验。
      msg.setStatus('思考中');
      break;
    case 'tool':
      msg.setStatus(`调用 ${ev.name}`);
      break;
    case 'sentence':
      msg.setStatus('生成中');
      await msg.appendSentence(ev.text, ev.kind);
      break;
    case 'blocked':
      msg.appendBlocked(ev.text, ev.reason);
      break;
    case 'gap':
      msg.appendGap(ev.text);
      break;
    case 'citations':
      msg.appendCitations(ev.items || []);
      break;
    case 'note':
      msg.appendNote(ev.text);
      break;
    case 'error':
      msg.appendError(ev.message || '未知错误');
      break;
  }
}

/* ── 粘贴识别（需求 6.3/6.4）────────────────────────────
 *
 * 四条分流：装备文本 / PoB 码 / 官方天赋树链接 / 普通文本。
 *
 * 识别后**先给卡片让用户确认**，不直接分析。两个理由：
 *   1. 识别可能判错。判错了直接分析等于用户莫名收到一份不相干的报告，
 *      而他连「系统把这段当成装备了」都不知道
 *   2. 粘贴的可能只是问题的一部分（贴完装备还想补一句「值多少钱」），
 *      直接触发分析就把他的下文吞了
 */

/* PoB 分享码是 URL-safe Base64 的 zlib 数据，长且只含 [A-Za-z0-9_-]。
 * 阈值取 80：短于此的多半是别的东西，宁可漏判也不要把普通文本当成码。 */
const POB_CODE_RE = /^[A-Za-z0-9_-]{80,}={0,2}$/;
/* 与后端 `parsers/pob_link.py` 的白名单保持同一覆盖面。这里只负责识别并弹确认卡，
 * 真正的安全边界仍由后端逐站点白名单执行。 */
const POB_LINK_RE = /^https?:\/\/(?:www\.)?(?:(?:pobb\.in|pastebin\.com|pastebinp\.com|pob\.codes\/b|poe\.ninja\/(?:poe1\/)?pob|maxroll\.gg\/poe\/pob|rentry\.co|poedb\.tw\/pob)\/\S+|pathofexile\.com\/(?:fullscreen-)?passive-skill-tree\/\S+)$/i;
const PASSIVE_TREE_LINK_RE = /^https?:\/\/(?:www\.)?pathofexile\.com\/(?:fullscreen-)?passive-skill-tree\/\S+$/i;
/* 装备文本的判据：段落分隔线 + 固定开头两行之一。
 * 只看分隔线不够——Markdown 里的 --- 也会命中。 */
const ITEM_HEAD_RE = /^(?:Item Class|Rarity|物品类别|物品類別|稀有度)\s*[:：]/im;

function detectPasteKind(text) {
  const trimmed = text.trim();
  if (!trimmed) return 'text';
  if (PASSIVE_TREE_LINK_RE.test(trimmed)) return 'tree';
  if (POB_CODE_RE.test(trimmed) || POB_LINK_RE.test(trimmed)) return 'pob';
  if (/^-{3,}$/m.test(trimmed) && ITEM_HEAD_RE.test(trimmed)) return 'item';
  return 'text';
}

/** 粘贴内容的首行摘要，用于卡片与消息气泡，避免把二十行原文铺满屏幕。 */
function pasteSummary(text) {
  const lines = text.trim().split('\n').map((l) => l.trim()).filter(Boolean);
  const head = lines.slice(0, 3).join(' · ');
  return lines.length > 3 ? `${head} …（共 ${lines.length} 行）` : head;
}

function hidePasteCard() {
  el.pasteCard.hidden = true;
  el.pasteCard.innerHTML = '';
}

function showPasteCard(kind, text) {
  const isItem = kind === 'item';
  const isTree = kind === 'tree';
  el.pasteCard.hidden = false;
  el.pasteCard.innerHTML =
    `<div class="pc-head"><span class="pc-tag">${isItem ? '识别为装备文本' : isTree ? '识别为 PoE 天赋树' : '识别为 PoB Build'}</span>` +
    `<button class="pc-close" title="取消">×</button></div>` +
    `<div class="pc-body">${escapeHtml(pasteSummary(text))}</div>` +
    (!isItem
      ? '<div class="pc-note">将解析 Build，展示核心 16 项与防御补充体检。数值取自这份 PoB 自带的快照，' +
        '本项目只做判读，不会把它冒充为本地重算结果。</div>'
      : '') +
    `<div class="pc-actions"><button class="pc-primary">${isItem ? '解析并分析' : '解析并体检'}</button>` +
    (isItem ? '<button class="pc-compare">查询可比挂单</button>' : '') +
    '<button class="pc-ghost">当作普通提问发送</button></div>';

  el.pasteCard.querySelector('.pc-close').onclick = hidePasteCard;
  const asText = el.pasteCard.querySelector('.pc-ghost');
  if (asText) {
    asText.onclick = () => {
      hidePasteCard();
      el.input.value = text;
      autoGrow();
      el.input.focus();
    };
  }
  const primary = el.pasteCard.querySelector('.pc-primary');
  const compare = el.pasteCard.querySelector('.pc-compare');
  if (compare) {
    compare.onclick = () => {
      hidePasteCard();
      window.openRareComparables?.(text);
    };
  }
  if (primary) {
    primary.onclick = () => {
      hidePasteCard();
      if (isItem) analyzeItem(text);
      else analyzeBuild(text);
    };
  }
}

el.input.addEventListener('paste', (e) => {
  const text = (e.clipboardData || window.clipboardData)?.getData('text') || '';
  const kind = detectPasteKind(text);
  if (kind === 'text') return;   // 普通文本照常粘贴，不打扰
  e.preventDefault();
  showPasteCard(kind, text);
});

/** 调 /item 做装备分析。这条链路不经 LLM，结果完全可预测。 */
async function analyzeItem(text) {
  if (busy) return;
  busy = true;
  el.send.disabled = true;
  addUserMessage(`【装备文本】${pasteSummary(text)}`);
  const msg = addAssistantMessage();
  msg.setStatus('解析装备');
  try {
    const resp = await apiFetch('/item', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await resp.json().catch(() => null);
    if (resp.status === 422 && data && data.detail) {
      msg.appendParseFailure(data.detail);
    } else if (!resp.ok) {
      msg.appendError(`HTTP ${resp.status}`);
    } else {
      // 报告自带「### 引用」小节，这里改用结构化引用渲染，
      // 好让正文里的角标能点击跳转
      const [bodyText] = String(data.answer || '').split('\n### 引用\n');
      await msg.appendMarkdown(bodyText);
      if ((data.citations || []).length) msg.appendCitations(data.citations);
    }
  } catch (err) {
    msg.appendError(String(err.message || err));
  } finally {
    msg.finish(null);
    busy = false;
    el.send.disabled = false;
    refreshConversations();
  }
}

/** 把用户 Build 里的自由文本压成 Markdown 表格安全的单行。
 * HTML 安全仍由 `markdown()` 的先转义保证；这里处理的是竖线破坏列结构。 */
function buildText(value, fallback = '—') {
  const text = String(value ?? '').replace(/\s+/g, ' ').replace(/\|/g, '｜').trim();
  return text || fallback;
}

/** `/build` 的结构化响应 → 前端报告。全程不调 LLM，不产生推断。 */
function renderBuild(data) {
  const identity = [data.class_name, data.ascend_class_name].filter(Boolean).join(' / ') || '未知职业';
  const heading = data.input_type === 'passive_tree'
    ? '# PoE 天赋树体检'
    : data.input_type === 'character_api' ? '# PoE 官方角色体检'
    : data.input_type === 'screenshot' ? '# PoE 截图 Build 体检' : '# PoB Build 体检';
  const lines = [
    heading,
    '',
    `- 职业：${buildText(identity)}`,
    `- 等级：${buildText(data.level)}`,
    `- 盗贼选择：${buildText(data.bandit)}`,
    `- 万神殿：${buildText([data.pantheon_major, data.pantheon_minor].filter(Boolean).join(' / '))}`,
    `- 版本核对：${buildText(data.version_check && data.version_check.message)}`,
  ];

  if (data.input_type === 'passive_tree') {
    lines.push(
      `- 链接编码版本：${buildText(data.tree_serialization_version)}`,
      `- 升华编号：${buildText(data.ascendancy_id)}`,
      `- 副升华 / 血统编号：${buildText(data.secondary_ascendancy_id)}`
    );
  }
  if (data.input_type === 'character_api') {
    lines.push(
      `- 角色：${buildText(data.character_name)}`,
      `- 联盟：${buildText(data.character_league)}`,
      `- Realm：${buildText(data.character_realm)}`,
      `- 副升华 / Bloodline：${buildText(data.alternate_ascendancy_name)}`
    );
  }
  if (data.version_check && data.version_check.basis) {
    lines.push(`- 版本依据：${buildText(data.version_check.basis)}`);
  }

  lines.push('', '## 核心 16 项与防御补充体检', '');
  lines.push('| 项目 | 数值 | 判定 | 依据 |');
  lines.push('| --- | --- | --- | --- |');

  for (const check of data.report || []) {
    lines.push(
      `| ${buildText(check.label)} | ${check.known ? buildText(check.value) : '未知'} | ` +
      `${check.known ? buildText(check.verdict) : buildText(check.missing_reason)} | ` +
      `${buildText(check.basis)} |`
    );
  }

  lines.push('', '## 输出分析：单体 / 清图 / Boss', '');
  lines.push('| 场景 | 当前快照 | 判定 | 依据 |');
  lines.push('| --- | --- | --- | --- |');
  for (const check of data.output_report || []) {
    lines.push(
      `| ${buildText(check.label)} | ${check.known ? buildText(check.value) : '未知'} | ` +
      `${check.known ? buildText(check.verdict) : buildText(check.missing_reason)} | ` +
      `${buildText(check.basis)} |`
    );
  }

  lines.push('', '## 防御来源（装备侧可核对部分）', '');
  const sources = data.defence_sources || [];
  if (!sources.length) {
    lines.push('- 未从装备文本识别到可逐字核对的防御来源。');
  } else {
    lines.push('| 类别 | 部位 / 装备 | 来源类型 | 原文 |');
    lines.push('| --- | --- | --- | --- |');
    for (const source of sources) {
      const owner = [source.slot, source.item_name].filter(Boolean).join(' / ');
      lines.push(
        `| ${buildText(source.label)} | ${buildText(owner)} | ` +
        `${buildText(source.origin)} | ${buildText(source.detail)} |`
      );
    }
  }
  if (data.defence_sources_note) lines.push('', `> ${buildText(data.defence_sources_note)}`);
  if ((data.defence_sources_unresolved || []).length) {
    lines.push('', '尚不能拆分的来源：');
    for (const note of data.defence_sources_unresolved) lines.push(`- ${buildText(note)}`);
  }

  lines.push('', '## 优化建议（低 / 高预算）', '');
  const suggestions = data.suggestions || [];
  if (!suggestions.length) {
    lines.push('- 当前已知指标没有触发确定性建议；未知项不视为合格，也不会被猜成短板。');
  } else {
    lines.push('| 已知短板 | 预算 | 建议 | 预期收益 | 大致成本 | 假设 |');
    lines.push('| --- | --- | --- | --- | --- | --- |');
    for (const item of suggestions) {
      lines.push(
        `| ${buildText(item.weakness)} | ${buildText(item.tier)} | ${buildText(item.action)} | ` +
        `${buildText(item.expected_gain)} | ${buildText(item.estimated_cost)} | ` +
        `${buildText(item.assumptions)} |`
      );
    }
  }
  if (data.suggestions_note) lines.push('', `> ${buildText(data.suggestions_note)}`);

  if (data.analysis_note) lines.push('', `> ${buildText(data.analysis_note)}`);
  if (data.player_stats_note) lines.push('', `> ${buildText(data.player_stats_note)}`);

  lines.push('', '## 技能组');
  const groups = data.skill_groups || [];
  if (!groups.length) lines.push('', '- 未解析到技能组');
  for (const [index, group] of groups.entries()) {
    const flags = [group.slot, group.is_main ? '主技能' : '', group.enabled ? '' : '已禁用']
      .filter(Boolean).join(' · ');
    lines.push('', `### ${index + 1}. ${buildText(flags, '未命名技能组')}`);
    const gems = group.gems || [];
    if (!gems.length) lines.push('', '- 未解析到宝石');
    for (const gem of gems) {
      const name = gem.name_zh
        ? `${buildText(gem.name_zh)}（${buildText(gem.name_en)}）`
        : buildText(gem.name_en, '未知宝石');
      const attrs = [
        gem.level == null ? '' : `${gem.level} 级`,
        gem.quality == null ? '' : `${gem.quality}% 品质`,
        gem.enabled ? '' : '已禁用',
        gem.gem_id ? '' : '未桥接中文数据',
      ].filter(Boolean).join('，');
      lines.push(`- ${name}${attrs ? `：${attrs}` : ''}`);
    }
  }

  lines.push('', '## 装备');
  const items = data.items || [];
  if (!items.length) lines.push('', '- 未解析到装备');
  for (const item of items) {
    const title = [item.slot, item.name, item.base_type].filter(Boolean).map((x) => buildText(x)).join(' · ');
    const attrs = [
      item.rarity,
      item.item_level == null ? '' : `物品等级 ${item.item_level}`,
      `${(item.implicits || []).length} 条隐式`,
      `${(item.explicits || []).length} 条显式`,
    ].filter(Boolean).map((x) => buildText(x)).join('，');
    lines.push(`- ${buildText(title, '未命名装备')}：${attrs}`);
    if (item.error) lines.push(`  - 解析警告：${buildText(item.error)}`);
  }

  const notes = [...(data.report_notes || []), ...(data.warnings || [])];
  if (notes.length) {
    lines.push('', '## 注意事项', '');
    for (const note of notes) lines.push(`- ${buildText(note)}`);
  }
  return lines.join('\n');
}

/** 调 `/build` 解析 PoB、天赋树或 OAuth 账号中的角色，并显示确定性的体检报告。 */
async function analyzeBuild(code = '', characterName = '') {
  if (busy) return;
  busy = true;
  el.send.disabled = true;
  const isCharacter = Boolean(characterName.trim());
  const isTree = !isCharacter && PASSIVE_TREE_LINK_RE.test(code.trim());
  addUserMessage(
    isCharacter
      ? `【官方角色 API】${characterName.trim()}`
      : `${isTree ? '【PoE 天赋树】' : '【PoB Build】'}${pasteSummary(code)}`
  );
  const msg = addAssistantMessage();
  msg.setStatus(isCharacter ? '读取官方角色 API' : isTree ? '解析 PoE 天赋树' : '解析 PoB Build');
  try {
    const resp = await apiFetch('/build', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(isCharacter ? { character_name: characterName.trim() } : { code }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const detail = data && data.detail;
      msg.appendParseFailure(
        typeof detail === 'object'
          ? { reason: detail.reason || '无法解析这份 PoB Build' }
          : { reason: detail || `HTTP ${resp.status}` }
      );
    } else {
      await msg.appendMarkdown(renderBuild(data));
    }
  } catch (err) {
    msg.appendError(String(err.message || err));
  } finally {
    msg.finish(null);
    busy = false;
    el.send.disabled = false;
    el.input.focus();
  }
}

/* ── 截图识别（需求 6.5 / 8.1）─────────────────────────
 * 图片先交给已配置的视觉模型做受限 OCR，结果必须由用户确认；确认后的装备文本走
 * `/item`，Build 字段走 `/build/screenshot`。视觉模型不直接给游戏结论。
 */

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.readAsDataURL(file);
  });
}

function showScreenshotFailure(reason) {
  el.pasteCard.hidden = false;
  el.pasteCard.innerHTML =
    '<div class="pc-head"><span class="pc-tag">截图识别失败</span>' +
    '<button class="pc-close" title="关闭">×</button></div>' +
    `<div class="pc-note">${escapeHtml(reason)}</div>`;
  el.pasteCard.querySelector('.pc-close').onclick = hidePasteCard;
}

function screenshotBuildSummary(build) {
  const rows = [];
  if (build.level != null) rows.push(`等级：${build.level}`);
  if (build.class_name) rows.push(`职业：${build.class_name}`);
  if (build.ascend_class_name) rows.push(`升华：${build.ascend_class_name}`);
  if (build.main_skill) rows.push(`主技能：${build.main_skill}`);
  for (const [key, value] of Object.entries(build.player_stats || {})) {
    rows.push(`${key}：${value}`);
  }
  for (const [key, value] of Object.entries(build.player_stat_text || {})) {
    rows.push(`${key}：${value}`);
  }
  if (build.enemy_is_boss) rows.push(`敌人档位：${build.enemy_is_boss}`);
  return rows;
}

function showScreenshotCard(data, filename) {
  const kind = data.kind;
  const title = kind === 'item' ? '识别为装备截图'
    : kind === 'build' ? '识别为 Build 截图' : '无法确定截图类型';
  const uncertainty = (data.uncertain || []).length
    ? `<div class="pc-warning">待核对：${escapeHtml(data.uncertain.join('；'))}</div>` : '';
  let body = '';
  let action = '';
  if (kind === 'item') {
    body = `<textarea class="pc-transcript" aria-label="截图转录文本">${escapeHtml(data.transcript || '')}</textarea>`;
    action = '<button class="pc-primary">确认文字并分析装备</button>';
  } else if (kind === 'build') {
    const rows = screenshotBuildSummary(data.build || {});
    body = `<div class="pc-fields">${rows.map((row) => `<div>${escapeHtml(row)}</div>`).join('')}</div>`;
    action = '<button class="pc-primary">确认字段并体检 Build</button>';
  } else {
    body = `<div class="pc-fields">${escapeHtml(data.transcript || '没有识别出可用文字')}</div>`;
  }
  el.pasteCard.hidden = false;
  el.pasteCard.innerHTML =
    `<div class="pc-head"><span class="pc-tag">${title} · ${escapeHtml(filename)}</span>` +
    '<button class="pc-close" title="取消">×</button></div>' + body + uncertainty +
    '<div class="pc-note">视觉模型只做转录，可能出错；确认后才进入确定性分析。图片不会写入本地数据库。</div>' +
    (action ? `<div class="pc-actions">${action}<button class="pc-ghost">取消</button></div>` : '');

  el.pasteCard.querySelector('.pc-close').onclick = hidePasteCard;
  const cancel = el.pasteCard.querySelector('.pc-ghost');
  if (cancel) cancel.onclick = hidePasteCard;
  const primary = el.pasteCard.querySelector('.pc-primary');
  if (primary) {
    primary.onclick = () => {
      if (kind === 'item') {
        const transcript = el.pasteCard.querySelector('.pc-transcript').value.trim();
        if (!transcript) return;
        hidePasteCard();
        analyzeItem(transcript);
      } else {
        hidePasteCard();
        analyzeScreenshotBuild(data.build || {}, filename);
      }
    };
  }
}

async function recognizeScreenshot(file) {
  if (busy) return;
  if (file.size > 5 * 1024 * 1024) {
    showScreenshotFailure('图片超过 5 MB 上限');
    return;
  }
  busy = true;
  el.send.disabled = true;
  el.imageUpload.disabled = true;
  el.pasteCard.hidden = false;
  el.pasteCard.innerHTML = '<div class="pc-tag">正在识别截图…</div>';
  try {
    const dataUrl = await readDataUrl(file);
    const resp = await apiFetch('/screenshot/recognize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data_url: dataUrl, filename: file.name }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const detail = data && data.detail;
      showScreenshotFailure(
        typeof detail === 'object' ? detail.reason || `HTTP ${resp.status}`
          : detail || `HTTP ${resp.status}`
      );
    } else {
      showScreenshotCard(data, file.name);
    }
  } catch (err) {
    showScreenshotFailure(String(err.message || err));
  } finally {
    busy = false;
    el.send.disabled = false;
    el.imageUpload.disabled = false;
    el.imageInput.value = '';
  }
}

async function analyzeScreenshotBuild(build, filename) {
  if (busy) return;
  busy = true;
  el.send.disabled = true;
  addUserMessage(`【Build 截图】${filename}`);
  const msg = addAssistantMessage();
  msg.setStatus('核对截图可见字段');
  try {
    const resp = await apiFetch('/build/screenshot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...build, confirmed: true }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const detail = data && data.detail;
      msg.appendParseFailure({
        reason: typeof detail === 'object' ? detail.reason || '截图字段无法体检'
          : detail || `HTTP ${resp.status}`,
      });
    } else {
      await msg.appendMarkdown(renderBuild(data));
    }
  } catch (err) {
    msg.appendError(String(err.message || err));
  } finally {
    msg.finish(null);
    busy = false;
    el.send.disabled = false;
    el.input.focus();
  }
}

/* ── 会话 ─────────────────────────────────────────────── */

function conversationStorage() {
  // 已鉴权缓存跟随当前标签页，避免不同 API Key 的标签页互相暴露会话标题。
  return storedApiKey() ? sessionStorage : localStorage;
}

function savedConversations() {
  try {
    return JSON.parse(conversationStorage().getItem(CONVERSATION_STORE) || '[]');
  } catch {
    return [];
  }
}

function saveConversations(list) {
  conversationStorage().setItem(CONVERSATION_STORE, JSON.stringify(list.slice(0, 30)));
}

function refreshConversations() {
  if (!conversationId) return;
  const saved = savedConversations();
  const current = saved.find((c) => c.id === conversationId);
  const list = saved.filter((c) => c.id !== conversationId);
  const firstUser = el.thread.querySelector('.msg.user .body');
  list.unshift({
    id: conversationId,
    title: current && current.customTitle
      ? current.title
      : (firstUser ? firstUser.textContent : '新对话').slice(0, 60),
    customTitle: Boolean(current && current.customTitle),
  });
  saveConversations(list);
  renderConversationList();
}

function renderConversationList() {
  const list = savedConversations();
  const placeholder = conversationId ? '' : '<option value="" selected>（新对话）</option>';
  el.convs.innerHTML = placeholder + (list.length
    ? list.map((c) =>
        `<option value="${c.id}"${c.id === conversationId ? ' selected' : ''}>${escapeHtml(c.title)}</option>`
      ).join('')
    : (conversationId ? '<option value="">（无历史会话）</option>' : ''));
  el.renameChat.disabled = !conversationId;
  el.deleteChat.disabled = !conversationId;
}

async function syncConversations() {
  try {
    const resp = await apiFetch('/conversations?limit=30');
    if (!resp.ok) {
      if (resp.status !== 401) renderConversationList();
      return;
    }
    const data = await resp.json();
    const server = (data.items || []).map((item) => ({
      id: item.conversation_id,
      title: item.title || '新对话',
      customTitle: true,
    }));
    // 保留升级前仅存于浏览器的随机 ID；用户点开时服务端会安全地认领旧会话。
    const serverIds = new Set(server.map((item) => item.id));
    const legacy = savedConversations().filter((item) => !serverIds.has(item.id));
    saveConversations([...server, ...legacy]);
    renderConversationList();
  } catch {
    // 服务端暂不可用时继续使用本地缓存，不影响聊天。
    renderConversationList();
  }
}

async function loadConversation(id) {
  if (!id || busy) return;
  const resp = await apiFetch(`/conversations/${id}`);
  if (!resp.ok) {
    if (resp.status === 404) {
      saveConversations(savedConversations().filter((item) => item.id !== id));
      renderConversationList();
    }
    return;
  }
  const data = await resp.json();
  conversationId = id;
  el.thread.innerHTML = '';
  el.empty.style.display = 'none';
  for (const m of data.messages || []) {
    if (m.role === 'user') {
      addUserMessage(m.content);
    } else {
      const wrap = document.createElement('div');
      wrap.className = 'msg assistant';
      const body = document.createElement('div');
      body.className = 'body';
      body.innerHTML = markdown(m.content);
      wrap.appendChild(body);
      el.thread.appendChild(wrap);
    }
  }
  const firstUser = (data.messages || []).find((m) => m.role === 'user');
  const generatedTitle = (firstUser ? firstUser.content : '新对话').slice(0, 60);
  const saved = savedConversations();
  const current = saved.find((c) => c.id === id);
  const list = saved.filter((c) => c.id !== id);
  list.unshift({
    id,
    title: data.title || generatedTitle,
    customTitle: Boolean(current && current.customTitle)
      || Boolean(data.title && data.title !== generatedTitle),
  });
  saveConversations(list);
  renderConversationList();
  el.main.scrollTop = el.main.scrollHeight;
}

async function renameConversation() {
  if (!conversationId || busy) return;
  const current = savedConversations().find((c) => c.id === conversationId);
  const title = window.prompt('输入新的会话标题（最多 60 个字符）：', current?.title || '');
  if (title === null) return;
  const normalized = title.trim();
  if (!normalized || normalized.length > 60) {
    window.alert('会话标题需为 1–60 个字符。');
    return;
  }
  busy = true;
  try {
    const resp = await apiFetch(`/conversations/${conversationId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: normalized }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      window.alert((data && data.detail) || `重命名失败（HTTP ${resp.status}）`);
      return;
    }
    const list = savedConversations().filter((c) => c.id !== conversationId);
    list.unshift({ id: conversationId, title: data.title, customTitle: true });
    saveConversations(list);
    renderConversationList();
  } catch (err) {
    window.alert(`重命名失败：${String(err.message || err)}`);
  } finally {
    busy = false;
  }
}

async function deleteConversation() {
  if (!conversationId || busy) return;
  if (!window.confirm('删除当前会话及其消息？此操作不可撤销。')) return;
  busy = true;
  try {
    const resp = await apiFetch(`/conversations/${conversationId}`, {
      method: 'DELETE',
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      window.alert((data && data.detail) || `删除失败（HTTP ${resp.status}）`);
      return;
    }
    const deletedId = conversationId;
    conversationId = null;
    saveConversations(savedConversations().filter((item) => item.id !== deletedId));
    el.thread.innerHTML = '';
    el.empty.style.display = '';
    renderConversationList();
  } catch (err) {
    window.alert(`删除失败：${String(err.message || err)}`);
  } finally {
    busy = false;
  }
}

/* ── 输入框 ───────────────────────────────────────────── */

function autoGrow() {
  el.input.style.height = 'auto';
  el.input.style.height = Math.min(el.input.scrollHeight, 190) + 'px';
}

el.input.addEventListener('input', autoGrow);
el.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send(el.input.value);
  }
});
el.send.addEventListener('click', () => send(el.input.value));

el.imageUpload.addEventListener('click', () => {
  if (!busy) el.imageInput.click();
});
el.imageInput.addEventListener('change', () => {
  const file = el.imageInput.files && el.imageInput.files[0];
  if (file) recognizeScreenshot(file);
});

el.characterImport.addEventListener('click', () => {
  const name = window.prompt('输入当前 GGG OAuth 账号中的 PoE1 PC 角色名：');
  if (name && name.trim()) analyzeBuild('', name.trim());
});

el.newChat.addEventListener('click', () => {
  if (busy) return;
  conversationId = null;
  el.thread.innerHTML = '';
  el.empty.style.display = '';
  renderConversationList();
  el.input.focus();
});

el.renameChat.addEventListener('click', renameConversation);
el.deleteChat.addEventListener('click', deleteConversation);
el.convs.addEventListener('change', (e) => loadConversation(e.target.value));
el.authKey.addEventListener('click', () => showAuthDialog());
el.authForm.addEventListener('submit', saveApiKey);
el.authCancel.addEventListener('click', () => {
  el.authInput.value = '';
  el.authDialog.close();
});
el.authClear.addEventListener('click', clearApiKey);
el.authDialog.addEventListener('close', () => {
  el.authInput.value = '';
  el.authError.textContent = '';
  el.authError.hidden = true;
});
el.manageMemories.addEventListener('click', showMemoryDialog);
el.memoryClose.addEventListener('click', () => el.memoryDialog.close());
el.memoryReset.addEventListener('click', resetMemoryForm);
el.memoryForm.addEventListener('submit', saveMemory);
el.memoryList.addEventListener('click', (event) => {
  const button = event.target.closest('[data-memory-action]');
  if (!button || memoryBusy) return;
  const { memoryAction, memoryId } = button.dataset;
  if (memoryAction === 'edit') editMemory(memoryId);
  else if (memoryAction === 'toggle' || memoryAction === 'delete') {
    mutateMemory(memoryId, memoryAction);
  }
});
el.memoryDialog.addEventListener('close', () => {
  memoryEpoch += 1;
  memoryBusy = false;
  memoryItems = [];
  el.memoryList.replaceChildren();
  el.memoryStatus.textContent = '';
  resetMemoryForm();
});

document.querySelectorAll('.samples button').forEach((b) => {
  b.addEventListener('click', () => send(b.textContent));
});

updateAuthButton(false);
syncConversations();
el.input.focus();
