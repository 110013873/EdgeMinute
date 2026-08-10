// ============================================================
// 居中弹窗呈现器：把既有的 .popover 面板以「页面居中 + 遮罩」形式展示
// ------------------------------------------------------------
// 复用「新建会议」对话框的观感（暗色遮罩 + 轻微模糊 + 居中卡片），但不改动
// 各弹层自身的内容渲染逻辑。共享同一个遮罩层，同一时刻只展示一个居中弹层。
// 关闭方式：ESC、点击遮罩、点击弹层内 [data-modal-close]、或再次点击工具栏按钮。
// ============================================================
let backdrop = null;       // 共享遮罩层（懒创建）
let current = null;        // 当前展示的 .popover 元素（null = 无）
let onCloseCb = null;      // 关闭回调（可选）
const wired = new WeakSet(); // 已绑定 [data-modal-close] 委托的弹层，避免重复绑定

function ensureBackdrop() {
  if (backdrop) return backdrop;
  backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.hidden = true;
  backdrop.addEventListener('click', () => closeModal());
  document.body.appendChild(backdrop);
  return backdrop;
}

// 展示某个 .popover 为居中弹窗。onClose 在其关闭时回调一次。
export function openModal(pop, onClose) {
  if (current && current !== pop) closeModal();
  ensureBackdrop();
  current = pop;
  onCloseCb = onClose || null;
  backdrop.hidden = false;
  pop.classList.add('popover-modal');
  // 清除内联 display（HTML 里初始为 none），交由样式表决定：多数弹层默认 block，
  // 而 #contactsPop 需要 display:flex 以让列表内部滚动、底部添加区固定。若在此硬编码
  // block，内联样式会盖过 CSS 的 flex 布局，导致联系人过多时添加区被挤出屏幕。
  pop.style.display = '';
  // 委托关闭按钮（内容可能被 innerHTML 重渲染，故用委托、且每个弹层只绑一次）
  if (!wired.has(pop)) {
    pop.addEventListener('click', (e) => {
      if (e.target.closest('[data-modal-close]')) closeModal();
    });
    wired.add(pop);
  }
}

export function closeModal() {
  if (!current) return;
  const pop = current;
  current = null;
  pop.style.display = 'none';
  pop.classList.remove('popover-modal');
  if (backdrop) backdrop.hidden = true;
  const cb = onCloseCb;
  onCloseCb = null;
  if (cb) cb();
}

export function isModalOpen(pop) {
  return current === pop;
}

// ESC 关闭当前居中弹窗
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && current) closeModal();
});
