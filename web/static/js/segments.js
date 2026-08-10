// ============================================================
// 转写段：视图段映射（合并/筛选）、渲染、行内编辑与段操作
// ============================================================
import { state } from './state.js';
import { $, resultsScroll, emptyHint } from './dom.js';
import { escapeHtml, formatTime, joinText } from './util.js';
import { speakerLabel, speakerColor, hexToDim, refreshSpeakerFilter, renameSpeakerLabel } from './speakers.js';
import { seekAndPlay } from './player.js';
import { pushUndo } from './undo.js';
import { updateRunAllState } from './upload.js';
import { openChat, quoteSegmentToChat } from './chat.js';
import { openAnnotatePopover } from './voiceprint-annotate.js';
import { applySearchHighlight } from './find.js';
import { scheduleAutoSave } from './autosave.js';
import { toast } from './ui-feedback.js';
import { icon } from './icons.js';
import { attendeeList } from './meta-panel.js';

// ---------- 视图段：把原始 segments 按当前"合并/筛选"选项映射为渲染用的行 ----------
// 每个视图段带 srcIdx（覆盖的原始段下标数组）。未合并时 srcIdx 长度恒为 1。
export function isMergeOn() { return $('mergeToggle').checked; }

export function viewSegments(item) {
  const merge = isMergeOn();
  const out = [];
  item.segments.forEach((seg, idx) => {
    if (state.speakerFilter && String(seg.speaker) !== state.speakerFilter) return;
    const last = out[out.length - 1];
    if (merge && last && String(last.speaker) === String(seg.speaker) &&
        last.srcIdx[last.srcIdx.length - 1] === idx - 1) {
      // 与上一视图段连续且同发言人 → 合并（要求原始下标连续，避免筛选把不相邻段错误粘连）
      last.end = seg.end;
      last.text = joinText(last.text, seg.text);
      last.flagged = last.flagged || seg.flagged;
      last.srcIdx.push(idx);
    } else {
      out.push({ start: seg.start, end: seg.end, speaker: seg.speaker,
                 text: (seg.text || '').trim(), flagged: seg.flagged, srcIdx: [idx] });
    }
  });
  return out;
}

// ---------- 渲染结果 ----------
export function renderResults() {
  const withResults = state.files.filter(f => f.segments.length > 0);
  if (withResults.length === 0) {
    resultsScroll.classList.add('empty');
    resultsScroll.innerHTML = '';
    resultsScroll.appendChild(emptyHint);
    return;
  }
  resultsScroll.classList.remove('empty');
  resultsScroll.innerHTML = '';
  for (const item of withResults) {
    const views = viewSegments(item);
    const section = document.createElement('div');
    section.className = 'file-section';
    section.innerHTML = (withResults.length > 1 ? `<h2>${escapeHtml(item.file.name)}</h2>` : '') +
      `<div class="segments" data-file-id="${item.id}"></div>`;
    resultsScroll.appendChild(section);
    const segsEl = section.querySelector('.segments');
    if (views.length === 0) {
      const empty = document.createElement('p');
      empty.className = 'empty-hint';
      empty.style.padding = '8px 4px';
      empty.textContent = '该发言人没有发言内容';
      segsEl.appendChild(empty);
    } else {
      views.forEach(v => segsEl.appendChild(buildSegRow(item, v)));
    }
  }
  applySearchHighlight();
}

// view: {start,end,speaker,text,flagged,srcIdx:[...]}
export function buildSegRow(item, view) {
  const merged = view.srcIdx.length > 1;      // 该行是否由多个原始段合并而来
  const firstIdx = view.srcIdx[0];
  const row = document.createElement('div');
  row.className = 'seg' + (view.flagged ? ' flagged' : '') + (merged ? ' merged' : '');
  row.dataset.fileId = item.id;
  row.dataset.idx = firstIdx;
  row.dataset.srcIdx = view.srcIdx.join(',');
  row.dataset.start = view.start;
  row.dataset.end = view.end;
  row.style.borderLeftColor = speakerColor(view.speaker);
  // 合并行文本只读（避免把多段编辑安全地写回原始数据）；单段行仍可编辑
  const editable = merged ? '' : 'contenteditable="true"';
  row.innerHTML = `
    <div class="time">${formatTime(view.start)}–${formatTime(view.end)}</div>
    <div class="speaker" title="点击修改本段发言人" style="color:${speakerColor(view.speaker)};background:${hexToDim(speakerColor(view.speaker))};">${escapeHtml(speakerLabel(view.speaker))}</div>
    <div class="text" ${editable} spellcheck="false">${escapeHtml(view.text)}</div>
    <div class="rowbtns">
      <button class="rowbtn flag" title="标注声纹">${icon('flag', 15)}</button>
      <button class="rowbtn ai" title="AI问答">${icon('message', 15)}</button>
      <button class="rowbtn merge" title="与上一段合并"${merged ? ' disabled' : ''}>${icon('mergeUp', 15)}</button>
      <div class="rowmore">
        <button class="rowbtn more" title="更多操作" aria-haspopup="true" aria-expanded="false">${icon('more', 15)}</button>
        <div class="rowmenu" hidden>
          <button class="rowmenu-item split"${merged ? ' disabled' : ''}>${icon('split', 14)}<span>${merged ? '合并下不可拆分' : '在光标处拆分'}</span></button>
          <button class="rowmenu-item del">${icon('trash', 14)}<span>删除此段</span></button>
        </div>
      </div>
    </div>`;

  row.addEventListener('click', () => { seekAndPlay(item, view.start); setActiveSeg(row); });
  const textEl = row.querySelector('.text');
  if (!merged) {
    // 单段行文本可编辑：点击文本进入编辑（放光标），阻止冒泡以免同时触发行的跳转播放。
    textEl.addEventListener('click', (e) => e.stopPropagation());
    const seg = item.segments[firstIdx];
    textEl.addEventListener('blur', () => {
      const t = textEl.textContent;
      if (seg && t !== seg.text) { pushUndo(); seg.text = t; scheduleAutoSave(); }
      if (state.searchMatches.length) applySearchHighlight();
    });
  }
  // 合并行文本只读：不拦截点击，冒泡到行 → 与点时间列一致，跳转并播放该段音频。
  row.querySelector('.speaker').addEventListener('click', (e) => { e.stopPropagation(); beginSpeakerEdit(row.querySelector('.speaker'), view); });
  // 常用操作外置：标注声纹 / AI 提问
  row.querySelector('.rowbtn.flag').addEventListener('click', (e) => { e.stopPropagation(); openAnnotatePopover(item, item.segments[firstIdx], e.currentTarget); });
  row.querySelector('.rowbtn.ai').addEventListener('click', (e) => { e.stopPropagation(); openChat(); quoteSegmentToChat(item, view); });
  // 「与上一段合并」外置到按钮栏（AI问答右侧）：合并行禁用（多段行不可再合并）
  const mergeBtn = row.querySelector('.rowbtn.merge');
  if (!merged) mergeBtn.addEventListener('click', (e) => { e.stopPropagation(); mergeWithPrev(item, item.segments[firstIdx]); });
  // 「更多」菜单：低频操作（拆分 / 删除）收纳其中，点击 ⋯ 展开
  const moreBtn = row.querySelector('.rowbtn.more');
  const menu = row.querySelector('.rowmenu');
  const closeMenu = () => { menu.hidden = true; moreBtn.setAttribute('aria-expanded', 'false'); };
  moreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = menu.hidden;
    closeAllRowMenus();                       // 同一时刻只开一个行菜单
    menu.hidden = !open;
    moreBtn.setAttribute('aria-expanded', String(open));
  });
  const splitItem = menu.querySelector('.rowmenu-item.split');
  if (!merged) splitItem.addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); splitAtCursor(item, item.segments[firstIdx], textEl); });
  menu.querySelector('.rowmenu-item.del').addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); deleteView(item, view); });
  return row;
}

// 关闭所有已展开的行内「更多」菜单（打开新菜单或点击空白处时调用）
function closeAllRowMenus() {
  document.querySelectorAll('.rowmenu:not([hidden])').forEach(m => {
    m.hidden = true;
    const btn = m.parentElement?.querySelector('.rowbtn.more');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  });
}
// 点击页面任意其他位置时收起行菜单（挂一次即可）
document.addEventListener('click', (e) => {
  if (!e.target.closest || !e.target.closest('.rowmore')) closeAllRowMenus();
});

export function setActiveSeg(row) {
  if (state.activeSegEl) state.activeSegEl.classList.remove('active');
  row.classList.add('active');
  state.activeSegEl = row;
}

// ---------- 发言人就地重命名（点击说话人标签 → 变可编辑输入框，无弹窗） ----------
// 提交后把“当前显示名与本行相同”的所有原始发言人一并映射为新名字（走 speakerMap，
// 绝不改写 seg.speaker，与手动映射保持同一显示不变量）。
export function beginSpeakerEdit(spkEl, view) {
  if (spkEl.querySelector('input')) return;             // 已在编辑中
  const oldLabel = speakerLabel(view.speaker);
  const input = document.createElement('input');
  input.className = 'speaker-edit';
  input.value = oldLabel;
  input.spellcheck = false;
  input.autocomplete = 'off';

  // 参会人候选（去重）。原先用原生 <datalist>，但在 Chromium 上箭头极不显眼、
  // 且与 autocomplete=off 冲突被抑制，用户点“下拉”时看不到列表。改为自绘下拉面板：
  // 输入框仍可自由手输，下方 .spk-suggest 面板列出全部候选、随输入过滤、点击回填。
  const names = [...new Set(attendeeList())];

  // 编辑态放开 speaker 单元格的 overflow:hidden（否则会裁掉绝对定位的下拉面板）
  spkEl.classList.add('editing');
  spkEl.textContent = '';
  spkEl.appendChild(input);

  let suggest = null;      // 下拉面板（无候选则始终为 null）
  let activeIdx = -1;      // 键盘高亮项下标
  let userTyped = false;   // 用户是否已改动输入（未动过则展示全部候选，不按旧标签过滤）
  const renderSuggest = () => {
    if (!names.length) return;
    // 打开时输入框预填的是当前发言人标签（如“说话人0”），它不该当成过滤词——
    // 否则一进来就按它过滤，参会人一个都匹配不上，显示“无匹配参会人”。
    // 仅在用户真正键入后才按输入过滤。
    const kw = userTyped ? input.value.trim().toLowerCase() : '';
    const list = kw ? names.filter(n => n.toLowerCase().includes(kw)) : names;
    if (!suggest) {
      suggest = document.createElement('div');
      suggest.className = 'spk-suggest';
      spkEl.appendChild(suggest);
    }
    activeIdx = -1;
    suggest.innerHTML = list.map((n, i) =>
      `<button type="button" class="spk-suggest-item" data-i="${i}">${escapeHtml(n)}</button>`).join('')
      || '<div class="spk-suggest-empty">无匹配参会人</div>';
    suggest.hidden = false;
    // mousedown 优先于 input.blur：用 mousedown 提交，避免 blur 先触发把面板关掉
    suggest.querySelectorAll('.spk-suggest-item').forEach(btn => {
      btn.addEventListener('mousedown', (e) => {
        e.preventDefault();                 // 阻止输入框失焦
        input.value = list[+btn.dataset.i];
        commit(true);
      });
    });
  };
  if (names.length) {
    input.addEventListener('input', () => { userTyped = true; renderSuggest(); });
    // 聚焦即展开完整候选，无需先输入（这才是用户点“下拉”期望看到的）
    renderSuggest();
  }

  input.focus();
  input.select();

  let done = false;
  const commit = (save) => {
    if (done) return;
    done = true;
    const next = input.value.trim();
    if (save && next && next !== oldLabel) {
      pushUndo();
      renameSpeakerLabel(oldLabel, next);
      renderResults();                                  // 重建整个列表：所有同名行同步更新
      scheduleAutoSave();
    } else {
      // 取消或未变更：还原本标签文本并去掉编辑态（下拉面板随 textContent 清空一并移除）
      spkEl.classList.remove('editing');
      spkEl.textContent = oldLabel;
    }
  };
  input.addEventListener('click', (e) => e.stopPropagation());
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    const items = suggest && !suggest.hidden
      ? [...suggest.querySelectorAll('.spk-suggest-item')] : [];
    if (e.key === 'ArrowDown' && items.length) {
      e.preventDefault();
      activeIdx = (activeIdx + 1) % items.length;
      highlightSuggest(items, activeIdx);
    } else if (e.key === 'ArrowUp' && items.length) {
      e.preventDefault();
      activeIdx = (activeIdx - 1 + items.length) % items.length;
      highlightSuggest(items, activeIdx);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (activeIdx >= 0 && items[activeIdx]) input.value = items[activeIdx].textContent;
      commit(true);
    } else if (e.key === 'Escape') { e.preventDefault(); commit(false); }
  });
  input.addEventListener('blur', () => commit(true));
}

// 键盘上下键在下拉候选间移动高亮
function highlightSuggest(items, idx) {
  items.forEach((el, i) => el.classList.toggle('active', i === idx));
  items[idx]?.scrollIntoView({ block: 'nearest' });
}

// ---------- 段操作 ----------
// 删除视图段：删掉它覆盖的所有原始段
export function deleteView(item, view) {
  pushUndo();
  const drop = new Set(view.srcIdx);
  item.segments = item.segments.filter((_, i) => !drop.has(i));
  refreshSpeakerFilter();
  renderResults();
  updateRunAllState();
  scheduleAutoSave();
}

export function mergeWithPrev(item, seg) {
  const idx = item.segments.indexOf(seg);
  if (idx <= 0) return;
  pushUndo();
  const prev = item.segments[idx-1];
  const merged = { ...prev, end: seg.end, text: joinText(prev.text, seg.text), flagged: prev.flagged || seg.flagged };
  item.segments = item.segments.map((s,i) => i===idx-1 ? merged : s).filter((_,i) => i!==idx);
  renderResults();
  scheduleAutoSave();
}

// Ctrl+M：把当前聚焦/高亮的转写行与其上一段合并（合并行、首段等无效场景静默忽略）
export function mergeFocusedSeg() {
  if ($('tabTranscript').hidden) return;                 // 仅在语音转写页生效
  const row = state.activeSegEl || state.playingSegEl;
  if (!row || row.classList.contains('merged')) return;  // 无聚焦行或合并显示行不处理
  const item = state.files.find(f => f.id === row.dataset.fileId);
  if (!item) return;
  const seg = item.segments[+row.dataset.idx];
  if (seg) mergeWithPrev(item, seg);
}

export function splitAtCursor(item, seg, textEl) {
  const sel = window.getSelection();
  let pos = seg.text.length;
  if (sel && sel.rangeCount && textEl.contains(sel.anchorNode)) {
    const range = sel.getRangeAt(0).cloneRange();
    range.selectNodeContents(textEl);
    range.setEnd(sel.anchorNode, sel.anchorOffset);
    pos = range.toString().length;
  }
  if (pos <= 0 || pos >= seg.text.length) { toast('请把光标放在文本中间要拆分的位置', 'warn'); return; }
  pushUndo();
  const idx = item.segments.indexOf(seg);
  const mid = +(seg.start + (seg.end-seg.start) * (pos/seg.text.length)).toFixed(1);
  const first = { ...seg, end: mid, text: seg.text.slice(0,pos).trim() };
  const second = { ...seg, start: mid, text: seg.text.slice(pos).trim(), flagged: false };
  const arr = item.segments.slice();
  arr.splice(idx, 1, first, second);
  item.segments = arr;
  renderResults();
  scheduleAutoSave();
}
