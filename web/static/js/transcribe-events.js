// ============================================================
// 转写进度事件流（SSE 消费）：/history/{id}/events
// ------------------------------------------------------------
// 每个会议一条 EventSource；切换会议先关旧连。事件类型：
//   snapshot — 连接即发，各文件当前 status（进程内活动态覆盖落库态）
//   file     — 单文件状态变化（queued/processing/done/error）
//   meeting  — 会议级状态变化（draft/transcribing/done），刷新左栏角标
// done 帧不含 segments（保持帧小）：收到后 GET /history/{id} 拉回该文件段落。
// 服务端后台转写，故本连接断开不影响转写；页面刷新/重开会重连并收快照续上。
// ============================================================
import { state } from './state.js';
import { ensureSpeakerColors, refreshSpeakerFilter, applySpeakerMatches } from './speakers.js';
import { renderResults } from './segments.js';
import { renderFileList, updateRunAllState } from './upload.js';
import { scheduleAutoSave } from './autosave.js';

let es = null;          // 当前 EventSource
let esMeetingId = null; // 当前订阅的会议 id

export function disconnect() {
  if (es) { try { es.close(); } catch (e) {} es = null; }
  esMeetingId = null;
}

export function connect(meetingId) {
  disconnect();
  if (meetingId == null) return;
  esMeetingId = meetingId;
  es = new EventSource(`/history/${meetingId}/events`);
  es.onmessage = (e) => {
    if (esMeetingId !== meetingId || state.currentMeetingId !== meetingId) return;
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (ev.type === 'snapshot') applySnapshot(ev);
    else if (ev.type === 'file') applyFileEvent(ev);
    else if (ev.type === 'speaker_matches') applySpeakerMatchesEvent(ev);
    else if (ev.type === 'meeting') document.dispatchEvent(new CustomEvent('history:changed'));
  };
  es.onerror = () => { /* EventSource 会自动重连；无需处理 */ };
}

// 快照：把各文件 status 对齐到服务端（仅覆盖非终态本地项，避免抹掉已渲染段落）
function applySnapshot(ev) {
  const files = ev.files || [];
  let touched = false;
  for (const sf of files) {
    const item = state.files[sf.file_index];
    if (!item) continue;
    // 本地已是 done（有段落）则不因快照回退；其余以服务端为准
    if (item.status === 'done' && (item.segments || []).length) continue;
    if (item.status !== sf.status) { item.status = sf.status; touched = true; }
  }
  if (touched) { renderFileList(); updateRunAllState(); }
}

function applyFileEvent(ev) {
  const item = state.files[ev.file_index];
  if (!item) return;
  if (ev.status === 'done') {
    item.status = 'done';
    // 段落不随事件下发，回读该会议拉回本文件 segments
    pullSegments(ev.file_index);
  } else if (ev.status === 'error') {
    item.status = 'error';
    item.error = ev.error || '转写失败';
    renderFileList();
    updateRunAllState();
  } else {
    item.status = ev.status;  // queued / processing
    renderFileList();
    updateRunAllState();
  }
}

// 声纹自动匹配帧：静默把命中结果填进 speakerMap（仅未手填的簇），并持久化。
// segments 可能此刻尚未拉回（done 帧与本帧顺序不保证）——applySpeakerMatches 只写
// speakerMap，不依赖 segments；等 pullSegments 渲染时 speakerLabel 自会取到新名。
function applySpeakerMatchesEvent(ev) {
  console.log('[声纹匹配] 收到 SSE 帧', JSON.stringify(ev.matches), '当前 speakerMap', JSON.stringify(state.speakerMap));
  const changed = applySpeakerMatches(ev.matches || {});
  console.log('[声纹匹配] applySpeakerMatches changed=', changed, '写入后 speakerMap', JSON.stringify(state.speakerMap));
  if (!changed) return;
  refreshSpeakerFilter();
  renderResults();
  scheduleAutoSave();  // 自动填充结果并入自动保存，刷新后不丢
}

// 拉回单个文件的转写结果（done 帧后）
async function pullSegments(fileIndex) {
  const id = state.currentMeetingId;
  if (id == null) return;
  try {
    const res = await fetch(`/history/${id}`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (state.currentMeetingId !== id) return;  // 已切换
    const pf = ((data.item && data.item.payload && data.item.payload.files) || [])[fileIndex];
    const item = state.files[fileIndex];
    if (!pf || !item) return;
    item.segments = (pf.segments || []).map(s => ({
      start: s.start, end: s.end, speaker: s.speaker, text: s.text || '', flagged: !!s.flagged,
    }));
    item.elapsed = pf.elapsed ?? item.elapsed;
    item.status = 'done';
    item.error = '';
    ensureSpeakerColors();
    refreshSpeakerFilter();
    renderFileList();
    renderResults();
    updateRunAllState();
  } catch (e) { /* 拉取失败：状态仍为 done，用户可刷新重开 */ }
}
