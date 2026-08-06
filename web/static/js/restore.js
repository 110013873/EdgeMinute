// ============================================================
// 查看 / 恢复历史会议 + 新建会话（清空工作区）
// ------------------------------------------------------------
// 恢复 = 用历史 payload 整体替换当前工作区：
//   - 合成 state.files[]，音频 url 指向 /history/{id}/audio/{idx}（has_audio=false 则无音频，降级为纯文本）。
//   - meta / speakerMap / summary 一并还原，currentMeetingId=id。
//   - 直接写 state（不经编辑处理器），故不会触发自动保存回写。
//   - 重置 undo/redo（恢复态即新基线）。
// 监听 history.js 派发的 'history:open'（恢复）与 'history:new'（清空）。
// ============================================================
import { state, undoStack, redoStack, speakerColors, normalizeMeta } from './state.js';
import { gAudio } from './dom.js';
import { repopulateMeta } from './meta-panel.js';
import { ensureSpeakerColors, refreshSpeakerFilter } from './speakers.js';
import { renderResults } from './segments.js';
import { renderFileList, updateRunAllState } from './upload.js';
import { loadIntoPlayer, updatePlaybar } from './player.js';
import { renderSummary } from './summary.js';
import { updateUndoButtons } from './undo.js';
import { resetMeeting, bumpSaveEpoch } from './autosave.js';
import { syncWorkspaceVisibility } from './workspace.js';
import { connect as connectEvents, disconnect as disconnectEvents } from './transcribe-events.js';

// 释放当前工作区里由 URL.createObjectURL 生成的 blob，避免内存泄漏
function revokeBlobUrls() {
  for (const f of state.files) {
    if (f.url && f.url.startsWith('blob:')) { try { URL.revokeObjectURL(f.url); } catch (e) {} }
  }
}

// 把工作区重置为“空白会话”的公共步骤（不含 currentMeetingId 处理）
function resetWorkspaceState() {
  revokeBlobUrls();
  if (gAudio) { try { gAudio.pause(); } catch (e) {} gAudio.removeAttribute('src'); gAudio.load(); }
  state.files = [];
  state.currentItem = null;
  state.activeSegEl = null;
  state.playingSegEl = null;
  state.loopSeg = null;
  state.speakerMap = {};
  state.summary = null;
  state.summaryBusy = false;
  state.speakerFilter = '';
  state.searchMatches = [];
  state.searchPos = -1;
  // 会话切换：中止在途问答并清空对话（问答上下文绑定当前转写，不应跨会议残留）
  if (state.chatAbort) { try { state.chatAbort.abort(); } catch (e) {} state.chatAbort = null; }
  state.chatHistory = [];
  for (const k of Object.keys(speakerColors)) delete speakerColors[k];
  undoStack.length = 0;
  redoStack.length = 0;
}

function rerenderAll() {
  repopulateMeta();
  ensureSpeakerColors();
  refreshSpeakerFilter();
  renderFileList();
  renderResults();
  renderSummary();
  updateRunAllState();
  updateUndoButtons();
  updatePlaybar();
}

// 历史 payload.file → 运行期 state.files 项。
// status 用 payload 落库值（draft/transcribing 会议含 pending/queued/processing/
// error 文件），无 status 的旧记录按有无段落回退为 done/pending。
function payloadFileToRuntime(pf, meetingId, idx) {
  const segs = (pf.segments || []).map(s => ({
    start: s.start, end: s.end, speaker: s.speaker,
    text: s.text || '', flagged: !!s.flagged,
  }));
  const status = pf.status || (segs.length ? 'done' : 'pending');
  return {
    id: pf.id || ('h' + idx),
    file: { name: pf.name || `音频${idx + 1}`, size: 0 },  // 合成 File 占位，兼容既有渲染
    url: pf.has_audio ? `/history/${meetingId}/audio/${idx}` : null,
    status,
    error: pf.error || '',
    segments: segs,
    elapsed: pf.elapsed ?? null,
    duration: pf.duration ?? null,
    audio_name: pf.audio_name || '',
    has_audio: !!pf.has_audio,
  };
}

export async function restoreFromHistory(id) {
  // 作废任何在途保存的写回，避免其污染即将载入的历史工作区
  bumpSaveEpoch();
  let payload;
  try {
    const res = await fetch(`/history/${id}`);
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || 'HTTP ' + res.status);
    payload = (data.item && data.item.payload) || {};
  } catch (e) {
    alert('打开失败：' + (e.message || '网络错误'));
    return;
  }

  resetWorkspaceState();

  const pfiles = payload.files || [];
  state.files = pfiles.map((pf, idx) => payloadFileToRuntime(pf, id, idx));
  state.meta = normalizeMeta(payload.meta || {});
  state.speakerMap = payload.speakerMap || {};
  const sum = payload.summary;
  state.summary = (sum && Object.keys(sum).length) ? sum : null;
  state.currentMeetingId = id;

  rerenderAll();
  syncWorkspaceVisibility();

  // 载入首个有音频的文件（无音频则载首个，仅显示文本）
  const first = state.files.find(f => f.url) || state.files[0];
  if (first) loadIntoPlayer(first, false);

  // 订阅该会议的转写进度（草稿/转写中会实时收到状态，完成态收快照即静默）
  connectEvents(id);

  // 刷新左栏 active 高亮
  document.dispatchEvent(new CustomEvent('history:active', { detail: { id } }));
}

// 回到空态：清空工作区并解除记录关联（“新建”对话框取消后 / 删除当前会话后）
export function newSession() {
  disconnectEvents();
  resetWorkspaceState();
  state.meta = normalizeMeta({});
  resetMeeting();               // currentMeetingId = null
  rerenderAll();
  syncWorkspaceVisibility();
  document.dispatchEvent(new CustomEvent('history:active', { detail: { id: null } }));
}

export function initRestore() {
  document.addEventListener('history:open', (e) => restoreFromHistory(e.detail.id));
  // 回到空态由 'history:reset' 触发（删除当前会话后）；'history:new' 现由
  // new-meeting.js 接管（打开新建对话框），不再直接清空工作区。
  document.addEventListener('history:reset', newSession);
}
