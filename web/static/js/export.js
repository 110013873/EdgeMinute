// ============================================================
// 导出：会议纪要（后端模板渲染 .docx）/ 文字发言稿（纯前端 .txt）
// ============================================================
import { state } from './state.js';
import { $, downloadBtn } from './dom.js';
import { joinText } from './util.js';
import { speakerLabel } from './speakers.js';
import { ensureSummary } from './summary.js';
import { toast } from './ui-feedback.js';

function collectSegments(item) {
  const merge = $('optMergeSpk').checked;
  if (!merge) return item.segments.slice();
  const out = [];
  for (const seg of item.segments) {
    const last = out[out.length-1];
    if (last && String(last.speaker) === String(seg.speaker))
      out[out.length-1] = { ...last, end: seg.end, text: joinText(last.text, seg.text) };
    else out.push({ ...seg, text: seg.text.trim() });
  }
  return out;
}

// ---------- 文字发言稿 TXT ----------
// 每段块：发言人（单位）\n 发言内容 \n\n。单位取自参会人员中「姓名==发言人显示名」的那条，
// 匹配不到则不带括号。多文件时按文件顺序拼接，段间空行分隔。
function speakerUnit(label) {
  const name = (label || '').trim();
  const hit = (state.meta.attendees || []).find(a => (a.name || '').trim() === name);
  return hit ? (hit.unit || '').trim() : '';
}

function buildTranscriptText() {
  const withResults = state.files.filter(f => f.segments.length > 0);
  const blocks = [];
  for (const item of withResults) {
    if (withResults.length > 1) blocks.push(`【${item.file.name}】`);
    for (const seg of collectSegments(item)) {
      const label = speakerLabel(seg.speaker);
      const unit = speakerUnit(label);
      const head = unit ? `${label}（${unit}）` : label;
      const text = (seg.text || '').trim();
      blocks.push(`${head}\n${text}`);
    }
  }
  return blocks.join('\n\n') + '\n';
}

// ---------- 导出触发 ----------
function sanitizeName(s) { return String(s || '').replace(/[\\/:*?"<>|]/g, '_').trim(); }

function triggerDownload(blob, ext, baseName) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const base = sanitizeName(baseName) || 'transcript';
  a.download = base + '.' + ext;
  a.click();
  URL.revokeObjectURL(url);
}

// 文字发言稿：纯前端生成 .txt（逐段「发言人（单位）+ 换行 + 发言内容」，文件名=会议名称+发言稿）
function exportTranscript() {
  const withResults = state.files.filter(f => f.segments.length > 0);
  if (!withResults.length) { toast('没有可导出的转写内容', 'warn'); return; }
  const blob = new Blob([buildTranscriptText()], { type: 'text/plain;charset=utf-8' });
  const title = sanitizeName(state.meta.title) || '会议';
  triggerDownload(blob, 'txt', title + '-发言稿');
}

// 会议纪要：把当前数据发给后端，用 templates/minutes.docx 渲染
async function exportViaTemplate() {
  const withResults = state.files.filter(f => f.segments.length > 0);
  // 汇总所有文件的段落（多文件时顺序拼接）
  const segments = [];
  for (const item of withResults) for (const seg of item.segments) segments.push(seg);
  const options = {
    mergeSpeaker: $('optMergeSpk').checked,
    timestamp: $('optTimestamp').checked,
    speaker: $('optSpeaker').checked,
    speakerMap: state.speakerMap,
  };
  downloadBtn.disabled = true;
  const prev = downloadBtn.innerHTML;
  // 忙碌态文案统一带 spinner 前缀（与「导入议程」一致）
  const setBusy = (text) => { downloadBtn.innerHTML = `<span class="spinner"></span>${text}`; };
  try {
    // 会议总结（概要/各发言人观点/决议）：已生成则复用，未生成则自动调用接口生成后再导出。
    // 生成失败不阻断导出——退化为对应段落留空，保证仍能拿到 Word。
    let summaryPayload = null;
    try {
      setBusy(state.summary ? '生成中…' : '总结中…');
      const summary = await ensureSummary();
      if (summary) {
        summaryPayload = {
          overview: summary.overview || '',
          speakers: summary.speakers || [],
          resolutions: Array.isArray(summary.resolutions) ? summary.resolutions : (summary.resolutions || []),
        };
      }
    } catch (e) {
      // 总结失败仅记录，继续导出（概要/观点/决议为空）
    }
    setBusy('生成中…');
    const res = await fetch('/export/docx-template', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meta: state.meta, segments, options, summary: summaryPayload }),
    });
    if (!res.ok) {
      let msg = `导出失败（${res.status}）`;
      try { const j = await res.json(); if (j.error) msg = '导出失败：' + j.error; } catch (e) {}
      toast(msg, 'err');
      return;
    }
    triggerDownload(await res.blob(), 'docx', (sanitizeName(state.meta.title) || '会议') + '-会议纪要');
  } catch (e) {
    toast('导出失败：网络错误', 'err');
  } finally {
    downloadBtn.innerHTML = prev;
    downloadBtn.disabled = false;
  }
}

// 事件绑定（在 main.js 装配阶段调用一次）
export function initExport() {
  const exportOptsBtn = $('exportOptsBtn');
  const exportOptsPop = $('exportOptsPop');
  const exportMenuPop = $('exportMenuPop');
  const spkPop = $('spkPop');

  const closeExportMenu = () => { exportMenuPop.style.display = 'none'; };

  exportOptsBtn.addEventListener('click', () => {
    if (exportOptsPop.style.display === 'block') { exportOptsPop.style.display = 'none'; return; }
    const r = exportOptsBtn.getBoundingClientRect();
    exportOptsPop.style.display = 'block';
    exportOptsPop.style.top = (r.bottom + 6) + 'px';
    exportOptsPop.style.left = Math.max(8, r.right - 220) + 'px';
  });

  // 导出按钮：打开/关闭导出方式菜单
  downloadBtn.addEventListener('click', () => {
    if (exportMenuPop.style.display === 'block') { closeExportMenu(); return; }
    const r = downloadBtn.getBoundingClientRect();
    exportMenuPop.style.display = 'block';
    exportMenuPop.style.top = (r.bottom + 6) + 'px';
    exportMenuPop.style.left = Math.max(8, r.right - 200) + 'px';
  });

  $('exportMinutesItem').addEventListener('click', () => { closeExportMenu(); exportViaTemplate(); });
  $('exportTranscriptItem').addEventListener('click', () => { closeExportMenu(); exportTranscript(); });

  document.addEventListener('click', (e) => {
    if (exportOptsPop.style.display === 'block' && !exportOptsPop.contains(e.target) && e.target !== exportOptsBtn) exportOptsPop.style.display = 'none';
    if (exportMenuPop.style.display === 'block' && !exportMenuPop.contains(e.target) && e.target !== downloadBtn) closeExportMenu();
    if (spkPop.style.display === 'block' && !spkPop.contains(e.target) && e.target !== $('spkBtn')) spkPop.style.display = 'none';
  });
}
