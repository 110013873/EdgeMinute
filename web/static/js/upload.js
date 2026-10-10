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
import { $, dropzone, fileInput, fileListEl, runAllBtn, downloadBtn, gAudio } from './dom.js';
import { escapeHtml, escapeAttr, formatTime } from './util.js';
import { loadIntoPlayer, updatePlaybar } from './player.js';
import { persistFiles, uploadAudio } from './history-io.js';
import { icon } from './icons.js';
import { toast, confirmDialog } from './ui-feedback.js';
import { progressState, finishProgress } from './progress.js';

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

  // 上传+固化完成后自动开始转写：逐个提交本批成功上传（pending）的文件，
  // 无需用户手动点「开始转写」。已切换会议则跳过（避免给别的会议误提交）。
  if (state.currentMeetingId !== id) return;
  for (const item of added) {
    if (item.status === 'pending' && item.has_audio) await transcribeOne(item.id);
  }
}

export function renderFileList() {
  fileListEl.innerHTML = '';
  state.files.forEach((item, idx) => {
    // 墓碑槽位（已删除）不再渲染卡片。它必须留在 state.files 里——file_index 就是
    // 数组下标，重排会让所有音频 URL 与在途任务错位（见 CLAUDE.md）——但用户看不到。
    if (item.deleted || item.status === 'deleted') return;
    const div = document.createElement('div');
    div.className = 'file-item' + (state.currentItem === item ? ' playing' : '');
    div.dataset.id = item.id;
    div.dataset.index = idx;
    const busy = item.status === 'processing' || item.status === 'queued'
      || item.status === 'uploading' || item.status === 'recording';
    const prog = progressState(item);   // 转写进度条运行态（null=不显示）
    div.innerHTML = `
      <div class="row1">
        <span class="fname" title="${escapeAttr(item.file.name)}">${escapeHtml(item.file.name)}${item.file.size ? `<span class="fsize">/${(item.file.size/1024/1024).toFixed(1)}MB</span>` : ''}</span>
        <span class="badge ${item.status}">${statusLabel(item.status)}</span>
      </div>
      <div class="timing">${buildTimingHtml(item)}</div>
      ${prog ? `<div class="prog"><div class="prog-fill" style="width:${prog.width}"></div></div>` : ''}
      <div class="actions">
        <button class="btn-transcribe" data-id="${item.id}" ${busy ? 'disabled' : ''}>
          ${item.status === 'done' ? '重新转写' : '开始转写'}
        </button>
        <button class="btn-del-file" data-index="${idx}" title="删除这段录音"
                ${item.status === 'recording' ? 'disabled' : ''}>${icon('trash', 15)}</button>
      </div>
    `;
    div.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      loadIntoPlayer(item, false);
    });
    fileListEl.appendChild(div);
  });
  fileListEl.querySelectorAll('.btn-transcribe').forEach(btn =>
    btn.addEventListener('click', () => transcribeOne(btn.dataset.id)));
  fileListEl.querySelectorAll('.btn-del-file').forEach(btn =>
    btn.addEventListener('click', (e) => { e.stopPropagation(); deleteFile(Number(btn.dataset.index)); }));
}

export function statusLabel(s) {
  if (s === 'processing') return '<span class="spinner"></span>转写中…';
  if (s === 'queued') return '<span class="spinner"></span>排队中…';
  if (s === 'uploading') return '<span class="spinner"></span>上传中…';
  // 录制中：不用 spinner（不是「处理中」而是「采集进行中」），红点更贴切
  if (s === 'recording') return '<span class="rec-dot on"></span>录制中';
  return { pending:'待转写', done:'已完成', error:'失败', deleted:'已删除' }[s] || s;
}

// ---------- 删除单段录音 ----------
// 服务端**不重排数组**：删除 = 原地留一个墓碑槽位（status:'deleted'），下标空间保持
// 稳定，否则所有音频 URL 与在途转写任务都会错位（见 CLAUDE.md 的 file_index 说明）。
// 因此本地也照此办理：把 state.files[idx] 就地清空，而不是 splice 掉。
export async function deleteFile(index) {
  const item = state.files[index];
  if (!item) return;
  if (item.deleted || item.status === 'deleted') return;   // 已是墓碑：不重复确认
  const name = (item.file && item.file.name) || item.name || `第 ${index + 1} 段`;
  const ok = await confirmDialog({
    title: '删除录音',
    message: `确定删除「${name}」吗？该段录音与其转写文本会被永久删除，无法恢复。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;

  const id = state.currentMeetingId;
  if (id == null) return;
  try {
    const res = await fetch(`/history/${id}/files/${index}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  } catch (e) {
    toast('删除失败：' + (e.message || '网络错误'), 'err');
    return;
  }
  if (state.currentMeetingId !== id) return;
  applyTombstone(index);
  toast('已删除该段录音', 'ok');
}

// 把一个槽位就地改成墓碑并刷新界面（服务端已落库，这里只同步本地状态）。
export function applyTombstone(index) {
  const item = state.files[index];
  if (!item) return;
  const wasCurrent = state.currentItem === item;
  if (item.url && item.url.startsWith('blob:')) { try { URL.revokeObjectURL(item.url); } catch (e) {} }
  // 清掉该文件的进度条运行态：它按下标 id 挂在 progress.js 的 runs 表里，卡片没了
  // 也还是会被 rAF 每帧查一次（查不到 DOM 只是空转）。删除是终态，直接抹掉。
  finishProgress(item, false);
  Object.assign(item, {
    status: 'deleted', deleted: true, segments: [], url: null,
    audio_name: '', has_audio: false, duration: null, elapsed: null, error: '',
  });
  if (wasCurrent) {
    // 播放器正指着一个已删除的文件：换到剩下第一个非墓碑项；一个都不剩就整个清空。
    // 注意 loadIntoPlayer(null) 会直接 return（不清 currentItem、不摘 src），所以
    // 「全删光」这一支必须自己把三样东西都收拾干净，否则播放栏还挂着一个空标题、
    // 音频源还指在刚被 revoke 的 blob 上。
    const next = state.files.find(f => !f.deleted && f.status !== 'deleted');
    if (next) {
      loadIntoPlayer(next, false);
    } else {
      state.currentItem = null;
      state.playingSegEl = null;
      state.activeSegEl = null;
      gAudio.removeAttribute('src');
      gAudio.load();
      updatePlaybar();
    }
  }
  renderFileList();
  // 结果区/发言人筛选/播放栏/导出按钮的刷新由 main.js 统一监听 files:changed 处理，
  // 避免本模块反向 import segments.js 形成环。
  document.dispatchEvent(new CustomEvent('files:changed'));
}

export function updateRunAllState() {
  // 墓碑槽位不参与任何统计：它没有音频、没有段落，却会让 length 判断（如播放栏是否
  // 显示、导出是否有结果）以为会议里还有内容。
  const live = state.files.filter(f => !f.deleted && f.status !== 'deleted');
  const hasPending = live.some(f => f.status === 'pending' || f.status === 'error');
  runAllBtn.disabled = live.length === 0 || !hasPending;
  const hasResults = live.some(f => f.segments.length > 0);
  downloadBtn.disabled = !hasResults;
  const pb = $('playbar');
  if (pb) pb.style.display = live.length ? 'flex' : 'none';
}

export function buildTimingHtml(item) {
  const durPart = item.duration != null ? formatTime(item.duration) : '--:--:--';
  return item.elapsed != null ? `${durPart} / <span class="elapsed">${formatTime(item.elapsed)}</span>` : durPart;
}

// 提交后台转写任务（不等结果，状态经 SSE 回推）
export async function transcribeOne(id) {
  const item = state.files.find(f => f.id === id);
  if (!item) return;
  if (['processing', 'queued', 'uploading', 'recording'].includes(item.status)) return;
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
