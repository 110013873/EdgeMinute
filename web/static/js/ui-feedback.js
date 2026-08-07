// ============================================================
// 统一交互反馈：浮层 toast + 承诺式确认对话框（替换原生 alert / confirm）
// ------------------------------------------------------------
// - toast(text, kind): 轻量浮层提示，kind ∈ '' | 'ok' | 'err' | 'warn' | 'saving'。
//   复用 .save-toast 样式，自动漂浮出现、1.8s 后消失（saving 除外，需手动清）。
// - confirmDialog({...}): 返回 Promise<boolean>，居中模态；替代原生 confirm，
//   风格统一、可标注危险操作（danger）。焦点默认落在取消按钮，Esc/点击遮罩=取消。
// 无需在 HTML 预置节点，首次调用时惰性创建并复用。
// ============================================================

// —— 浮层 toast ——（与 autosave 的实现同源，抽到公共处供全站复用）
let toastEl = null;
export function toast(text, kind) {
  if (!toastEl) {
    toastEl = document.createElement('div');
    toastEl.className = 'save-toast';
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = text;
  toastEl.className = 'save-toast show' + (kind ? ' ' + kind : '');
  clearTimeout(toastEl._t);
  if (kind !== 'saving') {
    toastEl._t = setTimeout(() => { toastEl.className = 'save-toast'; }, 1800);
  }
}

// —— 承诺式确认对话框 ——（替代原生 confirm，可 await）
// opts: { title, message, confirmText='确定', cancelText='取消', danger=false }
let dlgEl = null;
export function confirmDialog(opts = {}) {
  const {
    title = '请确认',
    message = '',
    confirmText = '确定',
    cancelText = '取消',
    danger = false,
  } = opts;

  return new Promise((resolve) => {
    if (!dlgEl) {
      dlgEl = document.createElement('div');
      dlgEl.className = 'confirm-mask';
      document.body.appendChild(dlgEl);
    }
    dlgEl.innerHTML = `
      <div class="confirm-box" role="dialog" aria-modal="true">
        <div class="confirm-title">${escapeText(title)}</div>
        ${message ? `<div class="confirm-msg">${escapeText(message)}</div>` : ''}
        <div class="confirm-actions">
          <button class="confirm-cancel">${escapeText(cancelText)}</button>
          <button class="confirm-ok${danger ? ' danger' : ''}">${escapeText(confirmText)}</button>
        </div>
      </div>`;
    dlgEl.classList.add('show');

    const cleanup = (result) => {
      dlgEl.classList.remove('show');
      document.removeEventListener('keydown', onKey);
      dlgEl.onclick = null;
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }
      else if (e.key === 'Enter') { e.preventDefault(); cleanup(true); }
    };
    document.addEventListener('keydown', onKey);
    // 点击遮罩空白处 = 取消
    dlgEl.onclick = (e) => { if (e.target === dlgEl) cleanup(false); };
    dlgEl.querySelector('.confirm-cancel').addEventListener('click', () => cleanup(false));
    dlgEl.querySelector('.confirm-ok').addEventListener('click', () => cleanup(true));
    // 默认聚焦取消，避免误按回车执行危险操作
    dlgEl.querySelector('.confirm-cancel').focus();
  });
}

// 纯文本转义（对话框内容一律按文本渲染，杜绝注入）
function escapeText(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
