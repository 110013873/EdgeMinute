// ============================================================
// 自动保存：编辑防抖更新既有会议（PUT JSON）
// ------------------------------------------------------------
// B2 建档前移后，本模块只负责“更新既有记录”这一件事：
//   - 建档由 new-meeting.js 完成（POST /history, files:[], draft）。
//   - 音频落盘与后台转写由 upload.js 完成（POST audio / POST transcribe）。
//   - 转写结果回写由服务端后台任务完成（前端经 SSE 感知，不在此保存）。
// 因此这里只有一条路径：已建档（currentMeetingId!=null）时，把 meta / 文本 /
// 映射 / 总结 等前端可编辑内容防抖 PUT 回去。payload.files 保留各文件当前
// status 与 audio 引用（不再区分 done 与否），避免 PUT 抹掉草稿/转写中文件。
//   - 在途去重：保存进行中再来请求，置 dirty，结束后补跑一次。
//   - 会话切换防串写：resetMeeting/新建/恢复会 bump saveEpoch；await 后校验
//     epoch 未变才提示/派发，防止在途保存污染已切换的工作区。
// ============================================================
import { state } from './state.js';
import { buildPayload } from './history-io.js';

const DEBOUNCE_MS = 1500;

let saving = false;      // 是否有请求在途
let dirty = false;       // 在途期间又发生了变更
let debounceTimer = null;
let saveEpoch = 0;       // 每次工作区切换自增；在途保存据此判定是否已过期

// —— 轻量保存状态提示（无需预置 HTML，浮层 toast）——
let toastEl = null;
function toast(text, kind) {
  if (!toastEl) {
    toastEl = document.createElement('div');
    toastEl.className = 'save-toast';
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = text;
  toastEl.className = 'save-toast show' + (kind ? ' ' + kind : '');
  clearTimeout(toastEl._t);
  if (kind !== 'saving') {
    toastEl._t = setTimeout(() => { toastEl.className = 'save-toast'; }, 1800);
  }
}

// 已建档即可保存编辑（草稿也允许存 meta）
function hasSavableContent() {
  return state.currentMeetingId != null;
}

// 更新既有记录（纯 JSON，保留 audio_name 引用与各文件 status）
async function updateMeeting(epoch) {
  const id = state.currentMeetingId;
  const payload = buildPayload();
  const res = await fetch(`/history/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ payload }),
  });
  const data = await res.json();
  if (res.status === 404) {
    // 记录已被删除：解除关联，不再重建（建档由 new-meeting 显式发起）
    if (epoch === saveEpoch) state.currentMeetingId = null;
    throw new Error('记录已不存在');
  }
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
}

// 执行一次保存（仅更新）；在途去重；世代校验防串写。
export async function saveNow() {
  if (!hasSavableContent()) return;
  if (saving) { dirty = true; return; }
  saving = true; dirty = false;
  const epoch = saveEpoch;
  toast('保存中…', 'saving');
  try {
    await updateMeeting(epoch);
    if (epoch === saveEpoch) {
      toast('已保存', 'ok');
      document.dispatchEvent(new CustomEvent('history:changed'));
    }
  } catch (e) {
    if (epoch === saveEpoch) toast('保存失败：' + (e.message || '网络错误'), 'err');
  } finally {
    saving = false;
    if (dirty) { dirty = false; scheduleAutoSave(); }
  }
}

// 编辑后的防抖保存（多次快速编辑只触发最后一次）
export function scheduleAutoSave() {
  if (!hasSavableContent()) return;  // 未建档不保存（建档由 new-meeting 负责）
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => { debounceTimer = null; saveNow(); }, DEBOUNCE_MS);
}

// 新建会话：清空当前记录关联并 bump 世代（作废在途保存的写回）
export function resetMeeting() {
  state.currentMeetingId = null;
  saveEpoch++;
}

// 恢复历史：仅 bump 世代（作废在途保存），currentMeetingId 由恢复流程另行设为目标 id
export function bumpSaveEpoch() {
  saveEpoch++;
}

// 关闭/刷新前用 sendBeacon 冲刷待保存的“更新”（异步 fetch 在卸载期无法完成）。
function flushOnUnload() {
  if (!debounceTimer) return;
  clearTimeout(debounceTimer); debounceTimer = null;
  if (state.currentMeetingId == null) return;
  try {
    const body = new Blob([JSON.stringify({ payload: buildPayload() })], { type: 'application/json' });
    navigator.sendBeacon(`/history/${state.currentMeetingId}?beacon=1`, body);
  } catch (e) { /* 尽力而为 */ }
}

export function initAutosave() {
  window.addEventListener('pagehide', flushOnUnload);
  window.addEventListener('beforeunload', flushOnUnload);
}
