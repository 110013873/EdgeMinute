// ============================================================
// 撤销 / 重做：对 segments + speakerMap + meta 做整体快照
// ============================================================
import { state, undoStack, redoStack, UNDO_LIMIT, normalizeMeta } from './state.js';
import { $ } from './dom.js';
import { ensureSpeakerColors } from './speakers.js';
import { renderResults } from './segments.js';
import { repopulateMeta } from './meta-panel.js';
import { updateRunAllState } from './upload.js';
import { scheduleAutoSave } from './autosave.js';

export function snapshot() {
  return JSON.stringify({
    segments: state.files.map(f => ({ id:f.id, segs:f.segments })),
    speakerMap: state.speakerMap,
    meta: state.meta,
  });
}

export function restore(snap) {
  const data = JSON.parse(snap);
  // 墓碑槽位不参与撤销/重做：快照按下标+id 还原段落，若允许写回墓碑，一段已删除的
  // 录音会带着文本重新出现在结果区（服务端下次归一化会再把它清掉，表现为文字闪一下
  // 就没了）。删除是不可逆的，快照不该是它的后门。
  for (const s of data.segments) {
    const f = state.files.find(x => x.id === s.id);
    if (f && !f.deleted && f.status !== 'deleted') f.segments = s.segs;
  }
  state.speakerMap = data.speakerMap || {};
  state.meta = normalizeMeta(data.meta || state.meta);
  repopulateMeta();
  ensureSpeakerColors();
  renderResults();
  updateRunAllState();
  scheduleAutoSave();
}

export function pushUndo() {
  undoStack.push(snapshot());
  if (undoStack.length > UNDO_LIMIT) undoStack.shift();
  redoStack.length = 0;
  updateUndoButtons();
}

export function undo() { if (!undoStack.length) return; redoStack.push(snapshot()); restore(undoStack.pop()); updateUndoButtons(); }
export function redo() { if (!redoStack.length) return; undoStack.push(snapshot()); restore(redoStack.pop()); updateUndoButtons(); }
export function updateUndoButtons() { $('undoBtn').disabled = !undoStack.length; $('redoBtn').disabled = !redoStack.length; }
