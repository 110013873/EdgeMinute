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
import { renderFileList, updateRunAllState, applyTombstone } from './upload.js';
import { scheduleAutoSave } from './autosave.js';
import { startProgress, finishProgress, calibrateSpeed, clearAllProgress } from './progress.js';
import { noteRecordingFrame } from './recorder.js';

let es = null;          // 当前 EventSource
let esMeetingId = null; // 当前订阅的会议 id

export function disconnect() {
  if (es) { try { es.close(); } catch (e) {} es = null; }
  esMeetingId = null;
  clearAllProgress();  // 切会议清空进度运行态：fileId 跨会议不唯一，避免误命中新卡片
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
  // 快照里带 deleted 的槽位在本端可能是完好的（本端没连上时别的标签页删了它）：
  // 走 applyTombstone 走完整流程，而不是只改 status——否则 deleted 标志没置上，
  // 本地这份「半墓碑」会在下一次自动保存时被写成一张内容完好的卡片，删除复活。
  for (const sf of files) {
    if (sf.status !== 'deleted') continue;
    const item = state.files[sf.file_index];
    if (item && !item.deleted) applyTombstone(sf.file_index);
  }
  let touched = false;
  for (const sf of files) {
    if (sf.status === 'deleted') continue;   // 上面已处理
    const item = state.files[sf.file_index];
    if (!item) continue;
    // 本地已是 done（有段落）则不因快照回退；其余以服务端为准
    if (item.status === 'done' && (item.segments || []).length) continue;
    if (item.status !== sf.status) { item.status = sf.status; touched = true; }
    // 重连/刷新后若服务端仍在处理，按服务端下发的真实起点续跑进度条
    if (sf.status === 'processing') startProgress(item, sf.started_at);
  }
  // 录制中的文件不属于转写流程：它的「已录时长」由 recorder.js 计时器负责，
  // 这里只把按钮态对齐（快照里任一条目处于 recording 即视为本会议在录）。
  const rec = files.find(sf => sf.status === 'recording');
  noteRecordingFrame(rec ? 'recording' : '', rec && rec.started_at);
  if (touched) { renderFileList(); updateRunAllState(); }
}

function applyFileEvent(ev) {
  const item = state.files[ev.file_index];
  if (!item) return;
  // 除「录制中」外的任何文件帧都意味着采集已结束：停掉录音条计时器/轮询。
  // 停止录音的帧序是 recording → pending → queued → processing → done，
  // 首个 pending 帧即在这里收尾，不必等下一次轮询。
  if (ev.status !== 'recording') noteRecordingFrame('', 0);
  if (ev.status === 'deleted') {
    // 别的标签页删掉了这一段：本地也把它变成墓碑（同样不重排，下标保持稳定）。
    // 只有本端发起删除时才带 deleted=true 的 payload，这里补的是「另一端删的」。
    if (!item.deleted) applyTombstone(ev.file_index);
    return;
  }
  if (ev.status === 'done') {
    item.status = 'done';
    finishProgress(item, true);   // 进度补满 100% 并淡出
    // 段落不随事件下发，回读该会议拉回本文件 segments
    pullSegments(ev.file_index);
  } else if (ev.status === 'error') {
    item.status = 'error';
    item.error = ev.error || '转写失败';
    finishProgress(item, false);  // 直接清除进度条
    renderFileList();
    updateRunAllState();
  } else if (ev.status === 'recording') {
    // 录音起止（本端或别的端发起）：对齐录音条按钮与计时器
    noteRecordingFrame('recording', ev.started_at);
    item.status = ev.status;
    renderFileList();
    updateRunAllState();
  } else {
    item.status = ev.status;  // pending / queued / processing
    if (ev.status === 'processing') startProgress(item, ev.started_at);  // 起跑假进度（含真实起点）
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
    item.segments = (pf.segments || []).map(s => {
      const seg = {
        start: s.start, end: s.end, speaker: s.speaker, text: s.text || '', flagged: !!s.flagged,
      };
      // 行级发言人覆盖：转写刚完成时一般还没有，但重新转写/拉回时可能已存在
      const ov = (s.speakerName || '').trim();
      if (ov) seg.speakerName = ov;
      return seg;
    });
    item.elapsed = pf.elapsed ?? item.elapsed;
    item.status = 'done';
    item.error = '';
    calibrateSpeed(item);   // 用真实 (duration/elapsed) 校准自适应倍速
    ensureSpeakerColors();
    refreshSpeakerFilter();
    renderFileList();
    renderResults();
    updateRunAllState();
  } catch (e) { /* 拉取失败：状态仍为 done，用户可刷新重开 */ }
}
