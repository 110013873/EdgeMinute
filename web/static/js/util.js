// ============================================================
// 纯工具函数（无副作用、不依赖其它业务模块）
// ============================================================

export function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML;
}
export function escapeAttr(str) { return escapeHtml(str).replace(/"/g, '&quot;'); }

export function cssEsc(s) { return String(s).replace(/["\\]/g, '\\$&'); }

export function xmlEsc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

export function formatTime(seconds) {
  const s = Math.floor(seconds);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
  return String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(sec).padStart(2,'0');
}

// 拼接两段文本：仅当衔接处两侧都是 ASCII 字母/数字时才补空格，
// 中文之间、中文标点后不加空格，避免合并段落出现多余空格
export function joinText(a, b) {
  a = (a || '').trim(); b = (b || '').trim();
  if (!a) return b;
  if (!b) return a;
  const needSpace = /[A-Za-z0-9]$/.test(a) && /^[A-Za-z0-9]/.test(b);
  return a + (needSpace ? ' ' : '') + b;
}

// 当前本地时间 → datetime-local 需要的 "YYYY-MM-DDTHH:mm"
export function nowDatetimeLocal() {
  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// LLM 返回的自由格式日期 → datetime-local 需要的 "YYYY-MM-DDTHH:mm"；无法解析则留空。
// 供会议信息面板与新建对话框共用（导入议程回填日期）。
export function toDatetimeLocal(raw) {
  const s = (raw || '').trim();
  if (!s) return '';
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(s)) return s.slice(0, 16);
  const pad = n => String(n).padStart(2, '0');
  let m = s.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?/);
  if (!m) m = s.match(/(\d{4})年(\d{1,2})月(\d{1,2})日\s*(?:(\d{1,2})[:：](\d{2}))?/);
  if (!m) return '';
  const [, y, mo, d, h, mi] = m;
  return `${y}-${pad(+mo)}-${pad(+d)}T${pad(h ? +h : 0)}:${pad(mi ? +mi : 0)}`;
}

// 会议决议规范化为“每条一项”的字符串数组，与后端 services.normalize_resolutions 同规则。
// 兼容三种输入：数组（逐元素拆分/去序号）、字符串（旧数据/换行/行内序号拆条）、null。
// 每条剥离 LLM 误加的行首序号/列表符，展示时统一重新编号。
const _RES_INLINE_SPLIT = /\s+(?=(?:[（(]?\d+[)）]|\d+\s*[.、．]|[①-⑳]|[一二三四五六七八九十]+\s*[、.．]))/g;
const _RES_NUM_PREFIX = /^\s*(?:[（(]?\d+[)）]|\d+\s*[.、．]|[①-⑳]|[一二三四五六七八九十]+\s*[、.．]|[-•*]\s)\s*/;

export function normalizeResolutions(raw) {
  let chunks;
  if (Array.isArray(raw)) chunks = raw.map((x) => (x == null ? '' : String(x)));
  else if (raw == null) return [];
  else chunks = [String(raw)];

  const out = [];
  for (const chunk of chunks) {
    for (const line of chunk.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      for (const part of trimmed.replace(_RES_INLINE_SPLIT, '\n').split('\n')) {
        const item = part.trim().replace(_RES_NUM_PREFIX, '').trim();
        if (item) out.push(item);
      }
    }
  }
  return out;
}

// 决议规范为“1. 2. 3. 逐条换行”的字符串（供纯文本场景）。
export function formatResolutions(raw) {
  return normalizeResolutions(raw).map((item, i) => `${i + 1}. ${item}`).join('\n');
}

// 把决议渲染为重新编号、逐条换行的安全 HTML（先 escape 再拼序号与 <br>）。
export function renderResolutions(raw) {
  const items = normalizeResolutions(raw);
  if (!items.length) return '';
  return items.map((item, i) => `${i + 1}. ${escapeHtml(item)}`).join('<br>');
}

// 轻量 Markdown 渲染（无第三方依赖）。安全约定：先整体 escapeHtml，
// 再在已转义文本上套用格式标记，绝不注入模型返回的原始 HTML，杜绝注入。
export function renderMarkdown(src) {
  const text = String(src == null ? '' : src);
  // 先抽出代码块（```），避免其中内容被行内规则误伤；用占位符回填
  const blocks = [];
  let s = text.replace(/```(\w+)?\n?([\s\S]*?)```/g, (_, lang, code) => {
    const i = blocks.length;
    blocks.push(`<pre><code>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
    return ` B${i} `;
  });
  const inline = (t) => escapeHtml(t)
    .replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`)               // 行内代码（内容已被 escapeHtml）
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, txt, url) => `<a href="${url.replace(/"/g,'&quot;')}" target="_blank" rel="noopener">${txt}</a>`);

  const lines = s.split('\n');
  let html = '', list = null;               // list: 'ul' | 'ol' | null
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  for (let raw of lines) {
    const ph = raw.match(/^ B(\d+) $/);
    if (ph) { closeList(); html += blocks[+ph[1]]; continue; }
    if (/^\s*$/.test(raw)) { closeList(); continue; }
    let m;
    if ((m = raw.match(/^(#{1,4})\s+(.*)$/))) { closeList(); const lv = m[1].length; html += `<h${lv}>${inline(m[2])}</h${lv}>`; continue; }
    if (/^\s*([-*+])\s+/.test(raw)) { if (list !== 'ul') { closeList(); html += '<ul>'; list = 'ul'; } html += `<li>${inline(raw.replace(/^\s*[-*+]\s+/, ''))}</li>`; continue; }
    if (/^\s*\d+\.\s+/.test(raw)) { if (list !== 'ol') { closeList(); html += '<ol>'; list = 'ol'; } html += `<li>${inline(raw.replace(/^\s*\d+\.\s+/, ''))}</li>`; continue; }
    if (/^\s*>\s?/.test(raw)) { closeList(); html += `<blockquote>${inline(raw.replace(/^\s*>\s?/, ''))}</blockquote>`; continue; }
    if (/^\s*([-*_])\1{2,}\s*$/.test(raw)) { closeList(); html += '<hr>'; continue; }
    closeList();
    html += `<p>${inline(raw)}</p>`;
  }
  closeList();
  return html;
}
