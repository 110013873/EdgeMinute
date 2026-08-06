// ============================================================
// 右侧 Tab 切换（语音转写 / 会议总结）+ 工具栏（合并开关 / 发言人筛选）
// ============================================================
import { state } from './state.js';
import { $ } from './dom.js';
import { renderResults } from './segments.js';
import { renderSummary } from './summary.js';

export function switchTab(tab) {
  document.querySelectorAll('.rtab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $('tabTranscript').hidden = tab !== 'transcript';
  $('tabSummary').hidden = tab !== 'summary';
  if (tab === 'summary') renderSummary();
}

// 中间栏 Tab 切换：会议录音上传 / 会议议程
export function switchCenterTab(ctab) {
  document.querySelectorAll('.ctab').forEach(b => b.classList.toggle('active', b.dataset.ctab === ctab));
  $('ctabUpload').hidden = ctab !== 'upload';
  $('ctabAgenda').hidden = ctab !== 'agenda';
}

export function initTabs() {
  document.querySelectorAll('.rtab').forEach(btn =>
    btn.addEventListener('click', () => switchTab(btn.dataset.tab)));
  document.querySelectorAll('.ctab').forEach(btn =>
    btn.addEventListener('click', () => switchCenterTab(btn.dataset.ctab)));

  $('mergeToggle').addEventListener('change', renderResults);
  $('speakerFilter').addEventListener('change', (e) => {
    state.speakerFilter = e.target.value;
    renderResults();
  });
}
