// ============================================================
// 会议信息面板：标量字段、结构化参会人编辑器、议程导入
// ============================================================
import { state, normalizeMeta } from './state.js';
import { $ } from './dom.js';
import { escapeAttr, toDatetimeLocal } from './util.js';
import { scheduleAutoSave } from './autosave.js';
import { icon } from './icons.js';

const attendeeEditor = $('attendeeEditor');

export function renderAttendees() {
  attendeeEditor.innerHTML = (state.meta.attendees || []).map((a, i) => `
    <div class="attendee-row" data-i="${i}">
      <span class="a-grip" draggable="true" title="拖拽调整顺序">${icon('grip', 15)}</span>
      <input class="a-name"  data-f="name"  value="${escapeAttr(a.name)}"  placeholder="姓名">
      <input class="a-unit"  data-f="unit"  value="${escapeAttr(a.unit)}"  placeholder="单位">
      <input class="a-title" data-f="title" value="${escapeAttr(a.title)}" placeholder="职务">
      <button class="a-del" data-del="${i}" title="删除">${icon('x', 15)}</button>
    </div>`).join('');
}

// 用当前 meta 重刷面板所有控件
export function repopulateMeta() {
  document.querySelectorAll('.meta-panel [data-k]').forEach(inp => { inp.value = state.meta[inp.dataset.k] || ''; });
  renderAttendees();
}

// —— 供导出 / 发言人弹层复用的 meta 派生助手 ——
export function attendeeList() {
  return (state.meta.attendees || []).map(a => (a.name || '').trim()).filter(Boolean);
}
// 参会人结构化 → 展示多行字符串，每人一行：姓名（单位·职务）（与后端 attendees_to_lines 一致）
export function attendeeLines() {
  return (state.meta.attendees || []).map(a => {
    const name = (a.name || '').trim(); if (!name) return '';
    const extra = [a.unit, a.title].map(x => (x || '').trim()).filter(Boolean).join('·');
    return extra ? `${name}（${extra}）` : name;
  }).filter(Boolean);
}
export function hasMeta() {
  const m = state.meta;
  const scalarFilled = ['title','date','place','agenda'].some(k => (m[k] || '').trim());
  return scalarFilled || (m.attendees && m.attendees.length > 0);
}
// datetime-local 存储为 "YYYY-MM-DDTHH:mm"，展示时把 T 换成空格
export function displayDate() { return (state.meta.date || '').replace('T', ' '); }

// —— 参会人行拖拽排序 ——
// 委托绑定在 attendeeEditor 上（只挂一次）：拖拽源是行首把手 .a-grip，放置目标是 .attendee-row。
// 依据指针相对目标行中线，决定插到目标行之前/之后；顺序变更后以不可变方式重排 meta.attendees。
let dragFromIdx = null;
function wireAttendeeDrag() {
  attendeeEditor.addEventListener('dragstart', (e) => {
    const grip = e.target.closest('.a-grip'); if (!grip) return;
    const row = grip.closest('.attendee-row'); if (!row) return;
    dragFromIdx = +row.dataset.i;
    row.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    // Firefox 需要设置数据拖拽才会启动
    try { e.dataTransfer.setData('text/plain', String(dragFromIdx)); } catch (err) {}
  });
  attendeeEditor.addEventListener('dragover', (e) => {
    if (dragFromIdx === null) return;
    e.preventDefault();                       // 允许放置
    e.dataTransfer.dropEffect = 'move';
    const row = e.target.closest('.attendee-row');
    attendeeEditor.querySelectorAll('.attendee-row.drop-before, .attendee-row.drop-after')
      .forEach(r => r.classList.remove('drop-before', 'drop-after'));
    if (!row) return;
    const rect = row.getBoundingClientRect();
    const after = e.clientY > rect.top + rect.height / 2;
    row.classList.add(after ? 'drop-after' : 'drop-before');
  });
  attendeeEditor.addEventListener('drop', (e) => {
    if (dragFromIdx === null) return;
    e.preventDefault();
    const row = e.target.closest('.attendee-row');
    if (row) {
      const to = +row.dataset.i;
      const rect = row.getBoundingClientRect();
      const after = e.clientY > rect.top + rect.height / 2;
      let target = after ? to + 1 : to;       // 插入位置（未剔除源之前的下标）
      reorderAttendees(dragFromIdx, target);
    }
    clearDragCues();
  });
  attendeeEditor.addEventListener('dragend', clearDragCues);
}

function clearDragCues() {
  dragFromIdx = null;
  attendeeEditor.querySelectorAll('.dragging, .drop-before, .drop-after')
    .forEach(r => r.classList.remove('dragging', 'drop-before', 'drop-after'));
}

// 把 from 处的参会人移动到 to 处（to 为剔除前的目标下标）；不可变重建数组后重渲染
function reorderAttendees(from, to) {
  const arr = state.meta.attendees || [];
  if (from < 0 || from >= arr.length) return;
  const next = arr.slice();
  const [moved] = next.splice(from, 1);
  const insertAt = to > from ? to - 1 : to;   // 剔除源后目标下标左移一位
  if (insertAt === from) return;              // 位置未变，跳过
  next.splice(insertAt, 0, moved);
  state.meta = { ...state.meta, attendees: next };
  renderAttendees();
  scheduleAutoSave();
}

// 事件绑定（在 main.js 装配阶段调用一次）
export function initMetaPanel() {
  // 标量字段绑定（不含 attendees，其由结构化编辑器管理）
  document.querySelectorAll('.meta-panel [data-k]').forEach(inp => {
    inp.addEventListener('input', () => { state.meta = { ...state.meta, [inp.dataset.k]: inp.value }; scheduleAutoSave(); });
  });

  // 结构化参会人编辑器：事件委托，改写 / 删除，均以不可变方式更新 meta
  attendeeEditor.addEventListener('input', (e) => {
    const row = e.target.closest('.attendee-row'); if (!row) return;
    const i = +row.dataset.i, f = e.target.dataset.f, v = e.target.value;
    state.meta = { ...state.meta, attendees: state.meta.attendees.map((a, idx) => idx === i ? { ...a, [f]: v } : a) };
    scheduleAutoSave();
  });
  attendeeEditor.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-del]'); if (!btn) return;
    const i = +btn.dataset.del;
    state.meta = { ...state.meta, attendees: state.meta.attendees.filter((_, idx) => idx !== i) };
    renderAttendees();
    scheduleAutoSave();
  });
  $('addAttendeeBtn').addEventListener('click', () => {
    state.meta = { ...state.meta, attendees: [...state.meta.attendees, { name:'', unit:'', title:'' }] };
    renderAttendees();
    // 新增后滚到底部并聚焦新行姓名输入，方便连续录入
    attendeeEditor.scrollTop = attendeeEditor.scrollHeight;
    const rows = attendeeEditor.querySelectorAll('.attendee-row');
    rows[rows.length - 1]?.querySelector('.a-name')?.focus();
  });

  // 拖拽调整参会人顺序：以行首「⋮⋮」把手为拖拽源，行为放置目标，落点前后插入
  wireAttendeeDrag();

  // 导入议程：上传 Word → 后端 LLM 提取 → 合并填充
  const importAgendaBtn = $('importAgendaBtn');
  const agendaFile = $('agendaFile');
  const agendaStatus = $('agendaStatus');
  const setAgendaStatus = (text, kind) => {
    agendaStatus.textContent = text || '';
    agendaStatus.className = 'mi-status' + (kind ? ' ' + kind : '');
  };
  const importAgendaLabel = importAgendaBtn.innerHTML;  // 保存原始按钮文案以便恢复
  importAgendaBtn.addEventListener('click', () => agendaFile.click());
  agendaFile.addEventListener('change', async () => {
    const f = agendaFile.files && agendaFile.files[0];
    if (!f) return;
    importAgendaBtn.disabled = true;
    importAgendaBtn.innerHTML = '<span class="spinner"></span>解析中…';
    setAgendaStatus('正在调用大模型提取会议信息，请稍候…', 'loading');
    try {
      const fd = new FormData();
      fd.append('file', f);
      const res = await fetch('/import-agenda', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
      const ex = data.meta || {};
      // 合并策略：提取到的非空字段覆盖当前值，避免清空用户已填内容
      const merged = { ...state.meta };
      ['title', 'place', 'agenda'].forEach(k => { if ((ex[k] || '').trim()) merged[k] = ex[k].trim(); });
      const dl = toDatetimeLocal(ex.date);
      if (dl) merged.date = dl;
      if (Array.isArray(ex.attendees) && ex.attendees.length) merged.attendees = ex.attendees;
      state.meta = normalizeMeta(merged);
      repopulateMeta();
      scheduleAutoSave();
      let note = '已导入并填充会议信息';
      if (ex.date && !dl) note += '（日期格式无法识别，请手动填写）';
      setAgendaStatus(note, 'ok');
    } catch (e) {
      setAgendaStatus('导入失败：' + (e.message || '网络错误'), 'err');
    } finally {
      importAgendaBtn.disabled = false;
      importAgendaBtn.innerHTML = importAgendaLabel;  // 恢复原始按钮文案
      agendaFile.value = '';  // 允许重新选择同一文件
    }
  });

  renderAttendees();
}
