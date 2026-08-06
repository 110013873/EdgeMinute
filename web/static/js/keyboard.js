// ============================================================
// 全局快捷键
// ============================================================
import { openFindWidget, closeFindWidget, isFindOpen } from './find.js';
import { undo, redo } from './undo.js';
import { mergeFocusedSeg } from './segments.js';
import { nudge, jumpSeg, playPause } from './player.js';

export function initKeyboard() {
  document.addEventListener('keydown', (e) => {
    const inEditable = e.target.isContentEditable || ['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName);
    if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='f') { e.preventDefault(); openFindWidget(false); return; }
    if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='h') { e.preventDefault(); openFindWidget(true); return; }
    if (e.key==='Escape' && isFindOpen()) { e.preventDefault(); closeFindWidget(); return; }
    if ((e.ctrlKey||e.metaKey) && !e.shiftKey && e.key.toLowerCase()==='z') { e.preventDefault(); undo(); return; }
    if ((e.ctrlKey||e.metaKey) && (e.key.toLowerCase()==='y' || (e.shiftKey && e.key.toLowerCase()==='z'))) { e.preventDefault(); redo(); return; }
    if ((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==='m') { e.preventDefault(); mergeFocusedSeg(); return; }
    if ((e.ctrlKey||e.metaKey) && e.key==='ArrowLeft') { e.preventDefault(); nudge(-5); return; }
    if ((e.ctrlKey||e.metaKey) && e.key==='ArrowRight') { e.preventDefault(); nudge(5); return; }
    if (e.altKey && e.key==='ArrowUp') { e.preventDefault(); jumpSeg(-1); return; }
    if (e.altKey && e.key==='ArrowDown') { e.preventDefault(); jumpSeg(1); return; }
    if (e.key===' ' && !inEditable) { e.preventDefault(); playPause(); return; }
  });
}
