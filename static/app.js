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
  newChat: $('new-chat'), convs: $('conversations'),
};

let conversationId = null;
let busy = false;

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
  busy = true;
  el.send.disabled = true;
  addUserMessage(text);
  el.input.value = '';
  autoGrow();

  const msg = addAssistantMessage();
  try {
    const resp = await fetch(`${API}/chat/stream`, {
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
 * 三条分流：装备文本 / PoB 码 / 普通文本。
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
const POB_LINK_RE = /^https?:\/\/(?:www\.)?(?:pobb\.in|pastebin\.com|poe\.ninja\/pob)\/\S+$/i;
/* 装备文本的判据：段落分隔线 + 固定开头两行之一。
 * 只看分隔线不够——Markdown 里的 --- 也会命中。 */
const ITEM_HEAD_RE = /^(?:Item Class|Rarity|物品类别|物品類別|稀有度)\s*[:：]/im;

function detectPasteKind(text) {
  const trimmed = text.trim();
  if (!trimmed) return 'text';
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
  const known = kind === 'item';
  el.pasteCard.hidden = false;
  el.pasteCard.innerHTML =
    `<div class="pc-head"><span class="pc-tag">${known ? '识别为装备文本' : '识别为 PoB 码'}</span>` +
    `<button class="pc-close" title="取消">×</button></div>` +
    `<div class="pc-body">${escapeHtml(pasteSummary(text))}</div>` +
    (known
      ? '<div class="pc-actions"><button class="pc-primary">解析并分析</button>' +
        '<button class="pc-ghost">当作普通提问发送</button></div>'
      : '<div class="pc-note">PoB 码解析尚未实现（排在 V2）。现在可以当作普通提问发送，' +
        '但请注意：知识库里没有 PoB 数据，很可能得到缺据声明。</div>' +
        '<div class="pc-actions"><button class="pc-ghost">当作普通提问发送</button></div>');

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
  if (primary) {
    primary.onclick = () => {
      hidePasteCard();
      analyzeItem(text);
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
    const resp = await fetch(`${API}/item`, {
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

/* ── 会话 ─────────────────────────────────────────────── */

const STORE = 'poe_agent_convs';

function savedConversations() {
  try { return JSON.parse(localStorage.getItem(STORE) || '[]'); } catch { return []; }
}

function refreshConversations() {
  if (!conversationId) return;
  const list = savedConversations().filter((c) => c.id !== conversationId);
  const firstUser = el.thread.querySelector('.msg.user .body');
  list.unshift({
    id: conversationId,
    title: (firstUser ? firstUser.textContent : '新对话').slice(0, 40),
  });
  localStorage.setItem(STORE, JSON.stringify(list.slice(0, 30)));
  renderConversationList();
}

function renderConversationList() {
  const list = savedConversations();
  el.convs.innerHTML = list.length
    ? list.map((c) =>
        `<option value="${c.id}"${c.id === conversationId ? ' selected' : ''}>${escapeHtml(c.title)}</option>`
      ).join('')
    : '<option value="">（无历史会话）</option>';
}

async function loadConversation(id) {
  if (!id || busy) return;
  const resp = await fetch(`${API}/conversations/${id}`);
  if (!resp.ok) return;
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
  el.main.scrollTop = el.main.scrollHeight;
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

el.newChat.addEventListener('click', () => {
  if (busy) return;
  conversationId = null;
  el.thread.innerHTML = '';
  el.empty.style.display = '';
  renderConversationList();
  el.input.focus();
});

el.convs.addEventListener('change', (e) => loadConversation(e.target.value));

document.querySelectorAll('.samples button').forEach((b) => {
  b.addEventListener('click', () => send(b.textContent));
});

renderConversationList();
el.input.focus();
