// ============================================================
// 入口装配：绑定跨模块事件、执行各模块 init、跑一次初始化
// module 脚本为 defer，执行时 DOM 已就绪。
// ============================================================
import { $, dropzone, fileInput, runAllBtn } from './dom.js';
import { addFiles, transcribeAll, updateRunAllState } from './upload.js';
import {
  updatePlaybar, playPause, nudge, setRate, toggleLoop, jumpSeg, initPlayerSync,
} from './player.js';
import { state } from './state.js';
import { undo, redo } from './undo.js';
import { ensureSpeakerColors, refreshSpeakerFilter } from './speakers.js';
import { renderResults } from './segments.js';
import { initMetaPanel } from './meta-panel.js';
import { initHotwords, loadHotwords } from './hotwords.js';
import { initFind } from './find.js';
import { initSpkPop } from './spk-pop.js';
import { initContacts } from './contacts.js';
import { initAnnotatePopover } from './voiceprint-annotate.js';
import { initExport } from './export.js';
import { initChat } from './chat.js';
import { initTabs } from './tabs.js';
import { initKeyboard } from './keyboard.js';
import { initAutosave } from './autosave.js';
import { initHistory } from './history.js';
import { initRestore } from './restore.js';
import { initNewMeeting } from './new-meeting.js';
import { initRecorder } from './recorder.js';
import { syncWorkspaceVisibility } from './workspace.js';

// ---------- 上传区事件 ----------
dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('drag'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('drag'));
dropzone.addEventListener('drop', (e) => { e.preventDefault(); dropzone.classList.remove('drag'); addFiles(e.dataTransfer.files); });
fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
runAllBtn.addEventListener('click', transcribeAll);

// 文件列表变化（目前只有「删除单段录音」会派发）：转写结果、发言人筛选、播放栏与
// 导出按钮都要跟着刷新。由 upload.js 派发、这里消费，避免 upload ↔ segments 互相
// import 形成环（segments.js 已经 import 了 upload.js 的 updateRunAllState）。
document.addEventListener('files:changed', () => {
  ensureSpeakerColors();
  refreshSpeakerFilter();
  renderResults();
  updateRunAllState();
  updatePlaybar();
});

// ---------- 底部播放器按钮 ----------
$('pbBack').addEventListener('click', () => nudge(-5));
$('pbFwd').addEventListener('click', () => nudge(5));
$('pbRateUp').addEventListener('click', () => setRate(state.playbackRate + 0.1));
$('pbRateDown').addEventListener('click', () => setRate(state.playbackRate - 0.1));
$('pbLoop').addEventListener('click', toggleLoop);
$('pbPrev').addEventListener('click', () => jumpSeg(-1));
$('pbNext').addEventListener('click', () => jumpSeg(1));
initPlayerSync();

// ---------- 撤销 / 重做按钮 ----------
$('undoBtn').addEventListener('click', undo);
$('redoBtn').addEventListener('click', redo);

// ---------- 各模块自绑定 ----------
initMetaPanel();
initHotwords();
initFind();
initSpkPop();
initContacts();
initAnnotatePopover();
initExport();
initChat();
initTabs();
initKeyboard();
initAutosave();
initHistory();
initRestore();
initNewMeeting();
initRecorder();

// ---------- 初始化 ----------
updatePlaybar();
loadHotwords();
// 首屏未建档 → 显示空态引导（工作区隐藏），点「新建」进入建档流程
syncWorkspaceVisibility();
