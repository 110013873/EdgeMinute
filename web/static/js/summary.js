// ============================================================
// 会议总结（会议总结 Tab）：概要 / 各发言人观点 / 决议
// ============================================================
import { state, COLOR_PALETTE, summaryOpen, summarySpeakerColors } from './state.js';
import { $ } from './dom.js';
import { escapeHtml, renderMarkdown, renderResolutions } from './util.js';
import { hexToDim } from './speakers.js';
import { chatSegments } from './chat.js';
import { scheduleAutoSave } from './autosave.js';

const summaryView = $('summaryView');

function setSumStatus(view, text, kind) {
  const el = view.querySelector('.sum-status');
  if (el) { el.innerHTML = text || ''; el.className = 'sum-status' + (kind ? ' ' + kind : ''); }
}

// 依据总结中出现的发言人显示名稳定分配颜色（后端已按 speakerMap 映射为姓名）
function summarySpeakerColor(name) {
  const key = String(name || '');
  if (!summarySpeakerColors[key]) summarySpeakerColors[key] = COLOR_PALETTE[state.summaryColorSeq++ % COLOR_PALETTE.length];
  return summarySpeakerColors[key];
}

export function renderSummary() {
  const segs = chatSegments();
  const hasSegs = segs.length > 0;
  const busyLabel = '<span class="spinner"></span>生成中…';

  if (!state.summary && !state.summaryBusy) {
    summaryView.innerHTML = `
      <div class="sum-bar">
        <button id="sumGenBtn"${hasSegs ? '' : ' disabled'}>生成总结</button>
        <span class="sum-status"></span>
      </div>
      <div class="sum-empty">${hasSegs
        ? '点击「生成总结」，AI 将依据会议转写内容<br>提炼会议概要、各发言人观点与会议决议。'
        : '请先在左侧完成音频转写，<br>再生成会议总结。'}</div>`;
    summaryView.querySelector('#sumGenBtn')?.addEventListener('click', generateSummary);
    return;
  }

  // 有总结（或正在生成）：操作条 + 三块
  summaryView.innerHTML = `
    <div class="sum-bar">
      <button id="sumRegenBtn" class="regen"${state.summaryBusy ? ' disabled' : ''}>${state.summaryBusy ? busyLabel : '重新总结'}</button>
      <span class="sum-status"></span>
    </div>
    <div id="sumSections"></div>`;
  summaryView.querySelector('#sumRegenBtn')?.addEventListener('click', generateSummary);
  if (state.summaryBusy) setSumStatus(summaryView, '正在调用大模型生成总结，请稍候…', 'loading');

  const sec = summaryView.querySelector('#sumSections');
  if (!state.summary) return;   // busy 且尚无旧结果时，仅显示操作条
  const summary = state.summary;
  sec.appendChild(buildSumSection('overview', '📋 会议概要',
    summary.overview ? `<div class="bubble md">${renderMarkdown(summary.overview)}</div>` : '<span class="empty-note">（无概要）</span>',
    !summary.overview));
  sec.appendChild(buildSumSection('speakers', '🗣 各发言人观点', buildSpeakersHtml(summary.speakers),
    !(summary.speakers && summary.speakers.length)));
  const resolutionsHtml = renderResolutions(summary.resolutions);
  sec.appendChild(buildSumSection('resolutions', '✅ 会议决议',
    resolutionsHtml ? `<div class="bubble md">${resolutionsHtml}</div>` : '<span class="empty-note">（无明确决议）</span>',
    !resolutionsHtml));
}

function buildSumSection(key, title, bodyHtml, isEmpty) {
  const div = document.createElement('div');
  div.className = 'sum-section' + (summaryOpen[key] ? '' : ' collapsed');
  const sp = title.indexOf(' ');                 // 首个空格前为 emoji 图标，之后为标题文字
  const icon = sp > 0 ? title.slice(0, sp) : '';
  const label = (sp > 0 ? title.slice(sp + 1) : title).trim();
  div.innerHTML = `
    <div class="sum-head">
      <span class="chev">▾</span><span class="sum-icon">${icon}</span><span>${escapeHtml(label)}</span>
    </div>
    <div class="sum-body${isEmpty ? ' empty-note' : ''}">${bodyHtml}</div>`;
  div.querySelector('.sum-head').addEventListener('click', () => {
    summaryOpen[key] = !summaryOpen[key];
    div.classList.toggle('collapsed', !summaryOpen[key]);
  });
  return div;
}

function buildSpeakersHtml(speakers) {
  if (!speakers || !speakers.length) return '<span class="empty-note">（未提炼到发言人观点）</span>';
  return speakers.map(sp => {
    const color = summarySpeakerColor(sp.speaker);
    return `
    <div class="sum-speaker-card" style="border-left-color:${color};">
      <span class="sp-name" style="color:${color};background:${hexToDim(color)};">${escapeHtml(sp.speaker || '发言人')}</span>
      <div class="sp-points bubble md">${renderMarkdown(sp.points || '')}</div>
    </div>`;
  }).join('');
}

// 调用后端生成总结并写入 state（单一 fetch 收敛点）。
// 成功返回 state.summary；失败抛错。不负责 UI 渲染，供 UI 按钮与导出流程共用。
async function requestSummary() {
  const segments = chatSegments();
  if (!segments.length) throw new Error('没有可总结的转写内容');
  const res = await fetch('/summarize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ meta: state.meta, segments, speakerMap: state.speakerMap }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  state.summary = data.summary;
  scheduleAutoSave();
  return state.summary;
}

// 若已有总结则直接复用，否则自动生成。供导出流程调用，全程更新 summary 面板状态。
// 无可总结内容时返回 null（导出照常进行，概要/决议留空）。
export async function ensureSummary() {
  if (state.summary) return state.summary;
  if (!chatSegments().length) return null;
  if (state.summaryBusy) return state.summary;   // 面板正在生成：不重复触发，用现有结果
  state.summaryBusy = true;
  renderSummary();
  try {
    const summary = await requestSummary();
    return summary;
  } finally {
    state.summaryBusy = false;
    renderSummary();
  }
}

async function generateSummary() {
  if (state.summaryBusy) return;
  if (!chatSegments().length) { setSumStatus(summaryView, '没有可总结的转写内容', 'err'); return; }
  state.summaryBusy = true;
  renderSummary();
  try {
    await requestSummary();
    setSumStatus(summaryView, '已生成', 'ok');
  } catch (e) {
    setSumStatus(summaryView, '生成失败：' + (e.message || '网络错误'), 'err');
  } finally {
    state.summaryBusy = false;
    renderSummary();
  }
}
