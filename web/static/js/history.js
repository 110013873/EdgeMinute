// ============================================================
// 历史会议浏览（左栏）：按天分组列表 + 搜索 + 删除 + 新建
// ------------------------------------------------------------
// 数据来自 GET /history（后端已返回按天分组的 groups）。
// - 列表项点击派发 CustomEvent('history:open', {detail:{id}})，由 restore 模块接管恢复。
// - 删除走 DELETE /history/{id}（后端同步删音频）；删的是当前会话则 resetMeeting。
// - 监听 document 'history:changed'（自动保存后派发）自动刷新，保持列表最新。
// ============================================================
import { state } from './state.js';
import { $ } from './dom.js';
import { escapeHtml, escapeAttr, formatTime } from './util.js';
import { icon } from './icons.js';
import { toast, confirmDialog } from './ui-feedback.js';

const listEl = $('historyList');
const searchEl = $('historySearch');
const newBtn = $('historyNewBtn');

let searchTimer = null;
let lastQuery = '';

// duration（秒）→ 紧凑时长（mm:ss / h:mm:ss）
function fmtDuration(sec) {
  if (!sec || sec <= 0) return '';
  const s = Math.round(sec);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const pad = n => String(n).padStart(2, '0');
  return h ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
}

// meeting_date "YYYY-MM-DDTHH:mm" → "HH:mm"（仅显示时刻，日期已由分组标题给出）
function fmtItemTime(raw) {
  const s = String(raw || '').replace('T', ' ').trim();
  const m = s.match(/(\d{1,2}):(\d{2})/);
  return m ? `${m[1].padStart(2, '0')}:${m[2]}` : '';
}

function itemMetaLine(it) {
  const bits = [];
  const t = fmtItemTime(it.meeting_date);
  if (t) bits.push(`<span class="hp-time">${t}</span>`);
  if (it.segment_count) bits.push(`${it.segment_count}段`);
  if (it.speaker_count) bits.push(`${it.speaker_count}人`);
  const dur = fmtDuration(it.duration);
  if (dur) bits.push(dur);
  return bits.join(' · ');
}

// 会议级 status → 列表角标（done 完成态不显示角标，避免噪声）
function statusBadge(status) {
  if (status === 'draft') return '<span class="hp-badge draft">草稿</span>';
  if (status === 'transcribing') return '<span class="hp-badge transcribing"><span class="spinner"></span>转写中</span>';
  return '';
}

function buildItem(it) {
  const div = document.createElement('div');
  div.className = 'hp-item' + (state.currentMeetingId === it.id ? ' active' : '');
  div.dataset.id = it.id;
  const title = (it.title || '').trim() || '未命名会议';
  div.innerHTML = `
    <div class="hp-main">
      <div class="hp-title" title="${escapeAttr(title)}"><span class="hp-title-text">${escapeHtml(title)}</span>${statusBadge(it.status)}</div>
      <div class="hp-meta">${itemMetaLine(it)}</div>
    </div>
    <button class="hp-del" title="删除此会议（含音频）" data-del="${it.id}">${icon('trash', 15)}</button>`;
  div.addEventListener('click', (e) => {
    if (e.target.closest('[data-del]')) return;
    document.dispatchEvent(new CustomEvent('history:open', { detail: { id: it.id } }));
  });
  div.querySelector('[data-del]').addEventListener('click', (e) => {
    e.stopPropagation();
    deleteMeeting(it.id, title);
  });
  return div;
}

function renderGroups(groups) {
  listEl.innerHTML = '';
  const nonEmpty = (groups || []).filter(g => (g.items || []).length);
  if (!nonEmpty.length) {
    const p = document.createElement('p');
    p.className = 'hp-empty';
    p.innerHTML = lastQuery ? '没有匹配的会议' : '暂无历史会议<br>转写完成后会自动保存到这里';
    listEl.appendChild(p);
    return;
  }
  for (const g of nonEmpty) {
    const day = document.createElement('div');
    day.className = 'hp-day';
    day.innerHTML = `${icon('calendar', 13)}<span>${escapeHtml(g.date)}</span>`;
    listEl.appendChild(day);
    for (const it of g.items) listEl.appendChild(buildItem(it));
  }
}

export async function refreshHistory() {
  try {
    const url = '/history?page=1&page_size=100' + (lastQuery ? '&q=' + encodeURIComponent(lastQuery) : '');
    const res = await fetch(url);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'HTTP ' + res.status);
    renderGroups(data.groups);
  } catch (e) {
    listEl.innerHTML = `<p class="hp-empty">加载失败：${escapeHtml(e.message || '网络错误')}</p>`;
  }
}

async function deleteMeeting(id, title) {
  const ok = await confirmDialog({
    title: '删除会议',
    message: `确定删除会议「${title}」吗？\n转写记录与音频将一并删除，且无法恢复。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;
  try {
    const res = await fetch(`/history/${id}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'HTTP ' + res.status);
    // 删的是当前会话：回到空态（断 SSE + 清工作区 + 解除关联）
    if (state.currentMeetingId === id) document.dispatchEvent(new CustomEvent('history:reset'));
    refreshHistory();
  } catch (e) {
    toast('删除失败：' + (e.message || '网络错误'), 'err');
  }
}

export function initHistory() {
  searchEl.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      lastQuery = searchEl.value.trim();
      refreshHistory();
    }, 250);
  });

  newBtn.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('history:new'));
  });

  // 自动保存后刷新列表（新建档 / 更新时长·标题等都会变）
  document.addEventListener('history:changed', refreshHistory);
  // 恢复 / 新建后刷新列表以更新 active 高亮
  document.addEventListener('history:active', refreshHistory);

  refreshHistory();
}
