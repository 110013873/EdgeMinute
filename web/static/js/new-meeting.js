// ============================================================
// 新建会议对话框：填写议程信息即建档（标题必填），建档前移
// ------------------------------------------------------------
// 与旧流程（转写完成才建档）相反：点「新建」先弹对话框收集 meta，标题必填，
// 提交即 POST /history（files:[], status:'draft'）拿到 id，随后 dispatch
// 'history:open' 载入这条空草稿，进入工作区上传音频。
// 对话框自带一套独立字段（nm-*）与参会人编辑器，避免与议程 Tab 的 data-k
// 控件 id 冲突；导入议程复用 POST /import-agenda。
// ============================================================
import { normalizeMeta } from './state.js';
import { $ } from './dom.js';
import { escapeAttr, toDatetimeLocal, nowDatetimeLocal } from './util.js';
import { icon } from './icons.js';

const dialog = $('newMeetingDialog');
const editor = $('nmAttendeeEditor');

// 对话框内的临时参会人数组（结构化 [{name,unit,title}]）
let attendees = [];

function renderAttendees() {
  editor.innerHTML = attendees.map((a, i) => `
    <div class="attendee-row" data-i="${i}">
      <input class="a-name"  data-f="name"  value="${escapeAttr(a.name)}"  placeholder="姓名">
      <input class="a-unit"  data-f="unit"  value="${escapeAttr(a.unit)}"  placeholder="单位">
      <input class="a-title" data-f="title" value="${escapeAttr(a.title)}" placeholder="职务">
      <button type="button" class="a-del" data-del="${i}" title="删除">${icon('x', 15)}</button>
    </div>`).join('');
}

// 收集对话框字段 → 归一化 meta
function collectMeta() {
  return normalizeMeta({
    title: $('nm-title').value,
    date: $('nm-date').value,
    place: $('nm-place').value,
    agenda: $('nm-agenda').value,
    attendees,
  });
}

function resetDialog() {
  $('nm-title').value = '';
  $('nm-date').value = nowDatetimeLocal();
  $('nm-place').value = '';
  $('nm-agenda').value = '';
  attendees = [];
  renderAttendees();
  hideTitleErr();
  setAgendaStatus('', '');
}

function showTitleErr() { $('nmTitleErr').hidden = false; $('nm-title').classList.add('invalid'); }
function hideTitleErr() { $('nmTitleErr').hidden = true; $('nm-title').classList.remove('invalid'); }

function setAgendaStatus(text, kind) {
  const el = $('nmAgendaStatus');
  el.textContent = text || '';
  el.className = 'mi-status' + (kind ? ' ' + kind : '');
}

function openDialog() {
  resetDialog();
  dialog.showModal();
  setTimeout(() => $('nm-title').focus(), 0);
}

function closeDialog() {
  if (dialog.open) dialog.close();
}

// 建档：POST /history（空 files、draft 态），成功后进入这条草稿
async function createDraft() {
  const title = $('nm-title').value.trim();
  if (!title) { showTitleErr(); $('nm-title').focus(); return; }

  const meta = collectMeta();
  const payload = {
    schema_version: 2,
    meta,
    speakerMap: {},
    summary: {},
    files: [],
    status: 'draft',
  };

  const btn = $('nmCreateBtn');
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '建档中…';
  try {
    const fd = new FormData();
    fd.append('payload', JSON.stringify(payload));
    const res = await fetch('/history', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    closeDialog();
    // 刷新左栏 + 载入这条空草稿进入工作区
    document.dispatchEvent(new CustomEvent('history:changed'));
    document.dispatchEvent(new CustomEvent('history:open', { detail: { id: data.id } }));
  } catch (e) {
    setAgendaStatus('建档失败：' + (e.message || '网络错误'), 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// 导入议程：上传 Word → 后端 LLM 提取 → 填充对话框字段
async function importAgenda(file) {
  const btn = $('nmImportBtn');
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>解析中…';
  setAgendaStatus('正在调用大模型提取会议信息，请稍候…', 'loading');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch('/import-agenda', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    const ex = data.meta || {};
    if ((ex.title || '').trim()) { $('nm-title').value = ex.title.trim(); hideTitleErr(); }
    if ((ex.place || '').trim()) $('nm-place').value = ex.place.trim();
    if ((ex.agenda || '').trim()) $('nm-agenda').value = ex.agenda.trim();
    const dl = toDatetimeLocal(ex.date);
    if (dl) $('nm-date').value = dl;
    if (Array.isArray(ex.attendees) && ex.attendees.length) {
      attendees = normalizeMeta({ attendees: ex.attendees }).attendees;
      renderAttendees();
    }
    let note = '已导入并填充会议信息';
    if (ex.date && !dl) note += '（日期格式无法识别，请手动填写）';
    setAgendaStatus(note, 'ok');
  } catch (e) {
    setAgendaStatus('导入失败：' + (e.message || '网络错误'), 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = label;
  }
}

export function initNewMeeting() {
  // 「新建」入口（左栏按钮 + 空态按钮）：改为打开对话框，而非直接清空工作区
  document.addEventListener('history:new', openDialog);
  const emptyBtn = $('emptyNewBtn');
  if (emptyBtn) emptyBtn.addEventListener('click', () => document.dispatchEvent(new CustomEvent('history:new')));

  $('nmCreateBtn').addEventListener('click', createDraft);
  $('nmCancel').addEventListener('click', closeDialog);
  $('nmCancel2').addEventListener('click', closeDialog);

  // 标题输入即清除错误提示
  $('nm-title').addEventListener('input', hideTitleErr);

  // 参会人编辑器（事件委托）
  editor.addEventListener('input', (e) => {
    const row = e.target.closest('.attendee-row'); if (!row) return;
    const i = +row.dataset.i, f = e.target.dataset.f, v = e.target.value;
    attendees = attendees.map((a, idx) => idx === i ? { ...a, [f]: v } : a);
  });
  editor.addEventListener('click', (e) => {
    const del = e.target.closest('[data-del]'); if (!del) return;
    const i = +del.dataset.del;
    attendees = attendees.filter((_, idx) => idx !== i);
    renderAttendees();
  });
  $('nmAddAttendeeBtn').addEventListener('click', () => {
    attendees = [...attendees, { name: '', unit: '', title: '' }];
    renderAttendees();
  });

  // 导入议程
  const importBtn = $('nmImportBtn');
  const agendaFile = $('nmAgendaFile');
  importBtn.addEventListener('click', () => agendaFile.click());
  agendaFile.addEventListener('change', async () => {
    const f = agendaFile.files && agendaFile.files[0];
    if (f) await importAgenda(f);
    agendaFile.value = '';  // 允许重选同一文件
  });
}
