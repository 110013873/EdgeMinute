// ============================================================
// 底部播放器（唯一音频源 globalAudio）+ 播放-转写高亮同步
// ============================================================
import { state } from './state.js';
import { $, gAudio } from './dom.js';
import { renderFileList } from './upload.js';
import { setActiveSeg } from './segments.js';

export function loadIntoPlayer(item, autoplay) {
  if (!item) return;
  if (state.currentItem !== item) {
    state.currentItem = item;
    if (item.url) {
      gAudio.src = item.url;
      gAudio.load();
      gAudio.playbackRate = state.playbackRate;
      // 读取时长回填左侧
      gAudio.onloadedmetadata = () => {
        if (item.duration == null) { item.duration = gAudio.duration; renderFileList(); }
      };
    } else {
      // 历史记录未保存音频：清空音频源与旧的元数据回调，仅展示文本
      gAudio.onloadedmetadata = null;
      gAudio.removeAttribute('src');
      gAudio.load();
    }
    renderFileList();
    updatePlaybar();
  }
  if (autoplay && item.url) { gAudio.playbackRate = state.playbackRate; gAudio.play(); }
}

export function seekAndPlay(item, startSeconds) {
  const doSeek = () => { gAudio.currentTime = startSeconds; gAudio.playbackRate = state.playbackRate; gAudio.play(); };
  if (state.currentItem !== item) {
    loadIntoPlayer(item, false);
    gAudio.addEventListener('loadedmetadata', doSeek, { once: true });
  } else {
    doSeek();
  }
}

export function updatePlaybar() {
  const it = state.currentItem;
  const name = it ? ((it.file && it.file.name) || it.name || '') : '';
  const noAudio = it && !it.url ? '（无音频）' : '';
  $('nowFile').textContent = it ? (name + noAudio) : '未选择音频';
  $('rateVal').textContent = state.playbackRate.toFixed(1) + '×';
  $('pbLoop').classList.toggle('on', !!state.loopSeg);
}

export function playPause() {
  if (!state.currentItem) { const f = state.files[0]; if (f) loadIntoPlayer(f, true); return; }
  if (gAudio.paused) { gAudio.playbackRate = state.playbackRate; gAudio.play(); } else gAudio.pause();
}

export function nudge(sec) {
  if (!state.currentItem) return;
  gAudio.currentTime = Math.max(0, Math.min(gAudio.duration || 1e9, gAudio.currentTime + sec));
}

export function setRate(r) {
  state.playbackRate = Math.max(0.5, Math.min(2, +r.toFixed(2)));
  gAudio.playbackRate = state.playbackRate;
  updatePlaybar();
}

export function toggleLoop() {
  if (state.loopSeg) state.loopSeg = null;
  else {
    const el = state.playingSegEl || state.activeSegEl;
    if (!el) { alert('请先点击或播放某一段，再开启循环'); return; }
    state.loopSeg = { start: parseFloat(el.dataset.start), end: parseFloat(el.dataset.end) };
  }
  updatePlaybar();
}

export function jumpSeg(dir) {
  if (!state.currentItem) { const f = state.files.find(x => (x.segments || []).length); if (f) loadIntoPlayer(f, false); }
  if (!state.currentItem) return;
  const segsEl = document.querySelector(`.segments[data-file-id="${state.currentItem.id}"]`);
  if (!segsEl) return;
  const rows = [...segsEl.querySelectorAll('.seg')];
  if (!rows.length) return;
  let cur = rows.indexOf(state.playingSegEl);
  if (cur === -1) cur = rows.indexOf(state.activeSegEl);
  let next = cur === -1 ? 0 : Math.max(0, Math.min(rows.length-1, cur + dir));
  const row = rows[next];
  seekAndPlay(state.currentItem, parseFloat(row.dataset.start));
  setActiveSeg(row);
}

// timeupdate 高亮同步：注册到 gAudio（在 main.js 装配阶段调用一次）
export function initPlayerSync() {
  gAudio.addEventListener('timeupdate', () => {
    const t = gAudio.currentTime;
    if (state.loopSeg && t >= state.loopSeg.end) { gAudio.currentTime = state.loopSeg.start; return; }
    if (!state.currentItem) return;
    const segsEl = document.querySelector(`.segments[data-file-id="${state.currentItem.id}"]`);
    if (!segsEl) return;
    let found = null;
    segsEl.querySelectorAll('.seg').forEach(r => {
      if (t >= parseFloat(r.dataset.start) && t < parseFloat(r.dataset.end)) found = r;
    });
    if (found !== state.playingSegEl) {
      if (state.playingSegEl) state.playingSegEl.classList.remove('playing');
      state.playingSegEl = found;
      if (state.playingSegEl) { state.playingSegEl.classList.add('playing'); state.playingSegEl.scrollIntoView({ behavior:'smooth', block:'nearest' }); }
    }
  });
}
