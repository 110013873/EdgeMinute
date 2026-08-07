// ============================================================
// 上传 / 文件列表 / 转写（B2：音频落盘即持久化，转写走服务端后台任务）
// ------------------------------------------------------------
// 建档已前移（new-meeting.js）：进入本面板时 currentMeetingId 必不为 null。
// 添加文件流程：
//   1. 追加到 state.files（status:'pending'），并 PUT 固化 files 结构（占好索引槽）
//   2. 逐个 POST /history/{id}/audio/{idx} 落盘，回填 audio_name/has_audio
//   3. 再 PUT 固化音频引用
// 转写：POST /history/{id}/transcribe/{idx} 仅提交后台任务，文件状态由 SSE 回推
// （见 transcribe-events.js），此处不再本地跑 /transcribe。
// file_index 严格等于 state.files 下标，与服务端 payload.files 顺序一一对应。
// ============================================================
import { state } from './state.js';
import { $, dropzone, fileInput, fileListEl, runAllBtn, downloadBtn } from './dom.js';
import { escapeHtml, escapeAttr, formatTime } from './util.js';
import { loadIntoPlayer } from './player.js';
import { persistFiles, uploadAudio } from './history-io.js';
import { toast } from './ui-feedback.js';

export async function addFiles(fileListRaw) {
  const id = state.currentMeetingId;
  if (id == null) { toast('请先新建会议', 'warn'); return; }

  let firstNew = null;
  const added = [];
  for (const f of fileListRaw) {
    const fid = 'f' + (state.fileSeq++);
    const item = { id: fid, file: f, url: URL.createObjectURL(f), status: 'pending', segments: [], elapsed: null, duration: null };
    state.files.push(item);
    added.push(item);
    if (!firstNew) firstNew = item;
  }
  renderFileList();
  updateRunAllState();
  if (firstNew) loadIntoPlayer(firstNew, false);

  // 先固化 files 结构（占好索引槽），再逐个落盘音频，最后再固化 audio 引用
  try {
    await persistFiles();
    for (const item of added) {
      const idx = state.files.indexOf(item);
      if (idx < 0) continue;  // 期间被删/切换
      // 上传期间置为 uploading：按钮自动禁用（复用 renderFileList 的 busy 判断），
      // 显示“上传中…”。大文件（如 2.5h 音频）落盘耗时明显，这段窗口必须锁住转写入口。
      item.status = 'uploading';
      item.error = '';
      renderFileList();
      updateRunAllState();
      try {
        const audioName = await uploadAudio(id, idx, item.file);
        if (state.currentMeetingId !== id) return;  // 已切换会议
        item.audio_name = audioName;
        item.has_audio = true;
        item.status = 'pending';  // 上传完成：回落到待转写，按钮恢复可点
      } catch (e) {
        item.status = 'error';
        item.error = '音频上传失败：' + (e.message || '网络错误');
      }
      renderFileList();
      updateRunAllState();
    }
    if (state.currentMeetingId === id) await persistFiles();
  } catch (e) {
    /* 持久化失败：文件仍在本地列表，用户可重试添加 */
  }
}

export function renderFileList() {
  fileListEl.innerHTML = '';
  for (const item of state.files) {
    const div = document.createElement('div');
    div.className = 'file-item' + (state.currentItem === item ? ' playing' : '');
    div.dataset.id = item.id;
    const busy = item.status === 'processing' || item.status === 'queued' || item.status === 'uploading';
    div.innerHTML = `
      <div class="row1">
        <span class="fname" title="${escapeAttr(item.file.name)}">${escapeHtml(item.file.name)}${item.file.size ? `<span class="fsize">/${(item.file.size/1024/1024).toFixed(1)}MB</span>` : ''}</span>
        <span class="badge ${item.status}">${statusLabel(item.status)}</span>
      </div>
      <div class="timing">${buildTimingHtml(item)}</div>
      <div class="actions">
        <button class="btn-transcribe" data-id="${item.id}" ${busy ? 'disabled' : ''}>
          ${item.status === 'done' ? '重新转写' : '开始转写'}
        </button>
      </div>
    `;
    div.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      loadIntoPlayer(item, false);
    });
    fileListEl.appendChild(div);
  }
  fileListEl.querySelectorAll('.btn-transcribe').forEach(btn =>
    btn.addEventListener('click', () => transcribeOne(btn.dataset.id)));
}

export function statusLabel(s) {
  if (s === 'processing') return '<span class="spinner"></span>转写中…';
  if (s === 'queued') return '<span class="spinner"></span>排队中…';
  if (s === 'uploading') return '<span class="spinner"></span>上传中…';
  return { pending:'待转写', done:'已完成', error:'失败' }[s] || s;
}

export function updateRunAllState() {
  const hasPending = state.files.some(f => f.status === 'pending' || f.status === 'error');
  runAllBtn.disabled = state.files.length === 0 || !hasPending;
  const hasResults = state.files.some(f => f.segments.length > 0);
  downloadBtn.disabled = !hasResults;
  const pb = $('playbar');
  if (pb) pb.style.display = state.files.length ? 'flex' : 'none';
}

export function buildTimingHtml(item) {
  const durPart = item.duration != null ? formatTime(item.duration) : '--:--:--';
  return item.elapsed != null ? `${durPart} / <span class="elapsed">${formatTime(item.elapsed)}</span>` : durPart;
}

// 提交后台转写任务（不等结果，状态经 SSE 回推）
export async function transcribeOne(id) {
  const item = state.files.find(f => f.id === id);
  if (!item) return;
  if (['processing', 'queued', 'uploading'].includes(item.status)) return;
  const meetingId = state.currentMeetingId;
  const idx = state.files.indexOf(item);
  if (meetingId == null || idx < 0) return;
  if (!item.has_audio) { toast('音频尚未上传完成，请稍候再试', 'warn'); return; }

  const hotword = $('hotwordInput').value.replace(/\s+/g,' ').trim();
  // 乐观置为排队中（SSE 首帧/后续帧会纠正为准确状态）
  item.status = 'queued';
  item.error = '';
  renderFileList();
  updateRunAllState();

  try {
    const res = await fetch(`/history/${meetingId}/transcribe/${idx}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hotword }),
    });
    const data = await res.json();
    if (res.status === 409) return;  // 已在转写：忽略重复提交
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  } catch (e) {
    item.status = 'error';
    item.error = '提交失败：' + (e.message || '网络错误');
    renderFileList();
    updateRunAllState();
  }
}

export async function transcribeAll() {
  runAllBtn.disabled = true;
  const targets = state.files.filter(f => f.status === 'pending' || f.status === 'error');
  for (const item of targets) await transcribeOne(item.id);
  updateRunAllState();
}
