// ============================================================
// 查找 / 替换（非模态弹窗，仿主流编辑器）
// ============================================================
import { state, undoStack, redoStack, UNDO_LIMIT } from './state.js';
import { $ } from './dom.js';
import { escapeHtml } from './util.js';
import { renderResults } from './segments.js';
import { snapshot, updateUndoButtons, pushUndo } from './undo.js';

const findPop = $('findPop');
const searchInput = $('findSearchInput');
const replaceInput = $('findReplaceInput');

export function openFindWidget(withReplace) {
  findPop.style.display = 'flex';
  if (withReplace) findPop.classList.add('expanded');
  runSearch();
  searchInput.focus();
  searchInput.select();
}
export function closeFindWidget() {
  findPop.style.display = 'none';
  state.searchMatches = []; state.searchPos = -1;
  applySearchHighlight();      // 清除高亮
}
export function isFindOpen() { return findPop.style.display !== 'none'; }

export function runSearch() {
  const q = searchInput.value;
  state.searchMatches = []; state.searchPos = -1;
  if (q) {
    const lower = q.toLowerCase();
    for (const item of state.files) item.segments.forEach((seg, idx) => {
      if ((seg.text || '').toLowerCase().includes(lower)) state.searchMatches.push({ fileId:item.id, idx });
    });
  }
  applySearchHighlight();
  if (state.searchMatches.length) gotoMatch(1, true);
  updateSearchUI();
}

// 由原始段下标定位到当前渲染出的视图行（合并显示时该原始段可能落在某个合并行内）
function rowForMatch(m) {
  const rows = document.querySelectorAll(`.seg[data-file-id="${m.fileId}"]`);
  for (const r of rows) {
    const src = (r.dataset.srcIdx || r.dataset.idx || '').split(',');
    if (src.includes(String(m.idx))) return r;
  }
  return null;
}

export function applySearchHighlight() {
  const q = searchInput.value;
  document.querySelectorAll('.seg .text').forEach(el => {
    const raw = el.textContent;
    if (!q) { el.textContent = raw; return; }
    el.innerHTML = highlight(raw, q);
  });
  if (state.searchPos >= 0 && state.searchMatches[state.searchPos]) {
    const row = rowForMatch(state.searchMatches[state.searchPos]);
    if (row) { const mk = row.querySelector('.text mark'); if (mk) mk.classList.add('current'); }
  }
}

function highlight(text, q) {
  const lower = text.toLowerCase(), ql = q.toLowerCase();
  let out = '', i = 0;
  while (true) {
    const at = lower.indexOf(ql, i);
    if (at === -1) { out += escapeHtml(text.slice(i)); break; }
    out += escapeHtml(text.slice(i, at)) + '<mark>' + escapeHtml(text.slice(at, at+q.length)) + '</mark>';
    i = at + q.length;
  }
  return out;
}

export function gotoMatch(dir, keepPos) {
  if (!state.searchMatches.length) return;
  if (!keepPos) state.searchPos = (state.searchPos + dir + state.searchMatches.length) % state.searchMatches.length;
  else if (state.searchPos < 0) state.searchPos = 0;
  applySearchHighlight();
  const row = rowForMatch(state.searchMatches[state.searchPos]);
  if (row) row.scrollIntoView({ behavior:'smooth', block:'center' });
  updateSearchUI();
}

function updateSearchUI() {
  const has = state.searchMatches.length > 0;
  const q = searchInput.value;
  const count = $('findCount');
  count.textContent = !q ? '无结果' : (has ? `${state.searchPos+1} / ${state.searchMatches.length}` : '无结果');
  count.classList.toggle('none', !!q && !has);
  $('findNextBtn').disabled = !has;
  $('findPrevBtn').disabled = !has;
  $('replaceOneBtn').disabled = !has;
  $('replaceAllBtn').disabled = !has;
}

// 替换当前项：改写当前匹配段中的一处，然后跳到下一处
function replaceOne() {
  if (state.searchPos < 0 || !state.searchMatches[state.searchPos]) return;
  const q = searchInput.value;
  if (!q) return;
  const rep = replaceInput.value;
  const m = state.searchMatches[state.searchPos];
  const item = state.files.find(f => f.id === m.fileId);
  if (!item) return;
  const seg = item.segments[m.idx];
  if (!seg) return;
  const lower = seg.text.toLowerCase(), at = lower.indexOf(q.toLowerCase());
  if (at === -1) { runSearch(); return; }
  pushUndo();
  seg.text = seg.text.slice(0, at) + rep + seg.text.slice(at + q.length);
  renderResults();
  const keep = state.searchPos;
  runSearch();
  // 停留在原位置附近，继续替换下一处
  if (state.searchMatches.length) { state.searchPos = Math.min(keep, state.searchMatches.length - 1); gotoMatch(0, true); }
}

// 全部替换
function replaceAll() {
  const find = searchInput.value;
  if (!find) return;
  const rep = replaceInput.value;
  let count = 0;
  const snap = snapshot();
  for (const item of state.files) for (const seg of item.segments) {
    if (seg.text.includes(find)) { count += seg.text.split(find).length-1; seg.text = seg.text.split(find).join(rep); }
  }
  if (count > 0) {
    undoStack.push(snap);
    if (undoStack.length > UNDO_LIMIT) undoStack.shift();
    redoStack.length = 0;
    updateUndoButtons();
    renderResults();
    runSearch();
  }
  $('findCount').textContent = count > 0 ? `已替换 ${count} 处` : '未找到';
}

// 事件绑定（在 main.js 装配阶段调用一次）
export function initFind() {
  $('findToggleBtn').addEventListener('click', () => { isFindOpen() ? closeFindWidget() : openFindWidget(false); });
  $('findCloseBtn').addEventListener('click', closeFindWidget);
  $('fwExpand').addEventListener('click', () => {
    findPop.classList.toggle('expanded');
    if (findPop.classList.contains('expanded')) replaceInput.focus();
  });
  $('findNextBtn').addEventListener('click', () => gotoMatch(1));
  $('findPrevBtn').addEventListener('click', () => gotoMatch(-1));
  $('replaceOneBtn').addEventListener('click', replaceOne);
  $('replaceAllBtn').addEventListener('click', replaceAll);

  searchInput.addEventListener('input', runSearch);
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); e.shiftKey ? gotoMatch(-1) : gotoMatch(1); }
    else if (e.key === 'Escape') { e.preventDefault(); closeFindWidget(); }
  });
  replaceInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); replaceOne(); }
    else if (e.key === 'Escape') { e.preventDefault(); closeFindWidget(); }
  });
}
