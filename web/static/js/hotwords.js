// ============================================================
// 热词持久化
// ============================================================
import { $ } from './dom.js';

const hotwordInput = $('hotwordInput');
const hwSaveBtn = $('hwSaveBtn');
const hwStatus = $('hwStatus');

function setHwStatus(text, kind) {
  hwStatus.textContent = text;
  hwStatus.className = 'hw-status' + (kind ? ' ' + kind : '');
}

export async function loadHotwords() {
  try {
    const res = await fetch('/hotwords');
    const data = await res.json();
    if (data.ok && typeof data.hotword === 'string') hotwordInput.value = data.hotword;
  } catch (e) { /* 加载失败静默，不影响使用 */ }
}

async function saveHotwords() {
  hwSaveBtn.disabled = true;
  setHwStatus('保存中…', '');
  try {
    const res = await fetch('/hotwords', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hotword: hotwordInput.value }),
    });
    const data = await res.json();
    setHwStatus(data.ok ? '已保存' : ('保存失败：' + (data.error || '')), data.ok ? 'ok' : 'err');
  } catch (e) {
    setHwStatus('保存失败：网络错误', 'err');
  } finally {
    hwSaveBtn.disabled = false;
  }
}

export function initHotwords() {
  hwSaveBtn.addEventListener('click', saveHotwords);
  hotwordInput.addEventListener('input', () => setHwStatus('', ''));

  // 工具栏「🔤 热词」按钮：切换弹层，锚定到按钮下方
  const hwBtn = $('hwBtn');
  const hotwordPop = $('hotwordPop');
  hwBtn.addEventListener('click', () => {
    if (hotwordPop.style.display === 'block') { hotwordPop.style.display = 'none'; return; }
    setHwStatus('', '');
    const r = hwBtn.getBoundingClientRect();
    hotwordPop.style.display = 'block';
    hotwordPop.style.top = (r.bottom + 6) + 'px';
    hotwordPop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 320)) + 'px';
    hotwordInput.focus();
  });
  // 点击弹层外部关闭
  document.addEventListener('click', (e) => {
    if (hotwordPop.style.display !== 'block') return;
    if (hotwordPop.contains(e.target) || hwBtn.contains(e.target)) return;
    hotwordPop.style.display = 'none';
  });
}
