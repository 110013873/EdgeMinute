// ============================================================
// 空态 / 工作区 可见性切换
// ------------------------------------------------------------
// 未建档（currentMeetingId==null）显示空态引导；已建档显示工作区（中栏+右栏）。
// 由 restore.js 在每次 open/new 后调用 syncWorkspaceVisibility()，
// 使可见性始终跟随 state.currentMeetingId，无需各处手动切。
// ============================================================
import { state } from './state.js';
import { $ } from './dom.js';

export function syncWorkspaceVisibility() {
  const ws = $('workspace');
  const es = $('emptyState');
  const active = state.currentMeetingId != null;
  if (ws) ws.hidden = !active;
  if (es) es.hidden = active;
}
