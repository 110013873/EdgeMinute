// ============================================================
// 通讯录选人弹窗（可复用）：从通讯录多选联系人，返回 {name,unit,title}[]
// ------------------------------------------------------------
// 「会议信息 → 参会人员」与「新建会议对话框」共用。自建一个动态 <dialog>，
// 不占用 index.html。调用 pickContacts() 返回一个 Promise：
//   - 确认  → resolve 选中的联系人数组（原始 {name,unit,title}，未做去重）
//   - 取消/关闭 → resolve []
// 去重（同名+同单位跳过）由调用方负责——因为它才知道当前已有的参会人。
// ============================================================
import { fetchContacts } from './contacts.js';
import { escapeHtml, escapeAttr } from './util.js';
import { icon } from './icons.js';

let dlg = null;       // 动态创建的 <dialog>
let resolveFn = null; // 当前 Promise 的 resolve
let people = [];      // 拉到的联系人列表
let selected = null;  // Set<contactId>

function ensureDialog() {
  if (dlg) return;
  dlg = document.createElement('dialog');
  dlg.className = 'nm-dialog cp-dialog';
  document.body.appendChild(dlg);
  // 点击遮罩空白关闭（等同取消）
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close([]); });
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close([]); });
}

function close(result) {
  if (dlg && dlg.open) dlg.close();
  const fn = resolveFn;
  resolveFn = null;
  if (fn) fn(result);
}

function render() {
  const rows = people.length ? people.map(p => {
    const meta = [p.unit, p.title].map(s => (s || '').trim()).filter(Boolean).join(' · ');
    const checked = selected.has(p.id) ? ' checked' : '';
    const voice = p.has_voice
      ? `<span class="cp-voice" title="已录声纹">${icon('mic', 12)}</span>`
      : '';
    return `
      <label class="cp-row" data-id="${p.id}">
        <input type="checkbox" class="cp-check" data-id="${p.id}"${checked}>
        <span class="cp-name">${escapeHtml(p.name || '(未命名)')}</span>
        ${voice}
        <span class="cp-meta">${escapeHtml(meta || '—')}</span>
      </label>`;
  }).join('') : '<div class="cp-empty">通讯录为空，请先在工具栏「通讯录」中添加联系人。</div>';

  dlg.innerHTML = `
    <div class="nm-form">
      <div class="nm-head">${icon('users', 18)} 从通讯录导入参会人
        <button type="button" class="nm-x" id="cpClose" title="关闭">${icon('x', 18)}</button>
      </div>
      <div class="nm-body">
        <input class="cp-search" id="cpSearch" placeholder="搜索姓名 / 单位 / 职务" autocomplete="off">
        <div class="cp-list" id="cpList">${rows}</div>
      </div>
      <div class="nm-foot">
        <span class="cp-count" id="cpCount"></span>
        <button type="button" class="nm-btn-cancel" id="cpCancel">取消</button>
        <button type="button" class="nm-btn-ok" id="cpOk">导入所选</button>
      </div>
    </div>`;
  wire();
  updateCount();
}

function updateCount() {
  const el = dlg.querySelector('#cpCount');
  if (el) el.textContent = selected.size ? `已选 ${selected.size} 人` : '';
}

function applyFilter(q) {
  const kw = (q || '').trim().toLowerCase();
  dlg.querySelectorAll('.cp-row').forEach(row => {
    if (!kw) { row.style.display = ''; return; }
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(kw) ? '' : 'none';
  });
}

function wire() {
  dlg.querySelector('#cpClose')?.addEventListener('click', () => close([]));
  dlg.querySelector('#cpCancel')?.addEventListener('click', () => close([]));
  dlg.querySelector('#cpOk')?.addEventListener('click', confirmPick);
  dlg.querySelector('#cpSearch')?.addEventListener('input', (e) => applyFilter(e.target.value));
  dlg.querySelectorAll('.cp-check').forEach(cb => {
    cb.addEventListener('change', () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) selected.add(id); else selected.delete(id);
      updateCount();
    });
  });
}

function confirmPick() {
  const chosen = people.filter(p => selected.has(p.id))
    .map(p => ({ name: p.name || '', unit: p.unit || '', title: p.title || '' }));
  close(chosen);
}

// 打开选人弹窗。返回 Promise<Array<{name,unit,title}>>（取消为空数组）。
export async function pickContacts() {
  ensureDialog();
  selected = new Set();
  people = [];
  // 先弹出加载态
  dlg.innerHTML = `
    <div class="nm-form">
      <div class="nm-head">${icon('users', 18)} 从通讯录导入参会人</div>
      <div class="nm-body"><div class="cp-empty">加载中…</div></div>
    </div>`;
  dlg.showModal();

  const p = new Promise((resolve) => { resolveFn = resolve; });

  const { enabled, people: list, error } = await fetchContacts();
  // 弹窗可能已被关闭
  if (!dlg.open) return p;
  if (error) {
    dlg.innerHTML = `
      <div class="nm-form">
        <div class="nm-head">${icon('users', 18)} 从通讯录导入参会人
          <button type="button" class="nm-x" id="cpClose" title="关闭">${icon('x', 18)}</button>
        </div>
        <div class="nm-body"><div class="cp-empty">加载失败：${escapeHtml(error)}</div></div>
        <div class="nm-foot"><button type="button" class="nm-btn-cancel" id="cpCancel">关闭</button></div>
      </div>`;
    dlg.querySelector('#cpClose')?.addEventListener('click', () => close([]));
    dlg.querySelector('#cpCancel')?.addEventListener('click', () => close([]));
    return p;
  }
  people = list;
  render();
  const searchEl = dlg.querySelector('#cpSearch');
  if (searchEl) setTimeout(() => searchEl.focus(), 0);
  return p;
}
