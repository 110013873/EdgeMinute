// ============================================================
// 热词持久化
// ============================================================
import { $ } from './dom.js';
import { openModal, closeModal, isModalOpen } from './modal.js';

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

  // 工具栏「🔤 热词」按钮：以页面居中弹窗展示（参考「新建会议」观感）
  const hwBtn = $('hwBtn');
  const hotwordPop = $('hotwordPop');
  hwBtn.addEventListener('click', () => {
    if (isModalOpen(hotwordPop)) { closeModal(); return; }
    setHwStatus('', '');
    openModal(hotwordPop);
    hotwordInput.focus();
  });
}
