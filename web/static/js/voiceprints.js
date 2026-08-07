// ============================================================
// 声纹库管理面板（工具栏「⚑ 声纹库」触发）
// ------------------------------------------------------------
// 提前采集每人语音样本 → 上传建立声纹→人物映射；转写时后端据此自动匹配发言人。
// 本面板只做文件上传（不录音），提完向量后端即丢弃音频，仅存 192 维向量。
// 接口：GET/POST /voiceprints、PATCH/DELETE /voiceprints/{id}、
//       GET/POST/DELETE /voiceprints/{id}/samples[/{sid}]
// ============================================================
import { $ } from './dom.js';
import { escapeHtml, escapeAttr } from './util.js';
import { icon } from './icons.js';
import { confirmDialog } from './ui-feedback.js';

const vpPop = $('vpPop');

// 与后端 VOICEPRINT_SAMPLE_MAX_BYTES 对齐（20MB）：前端先 fail-fast，避免整包上传后才被拒
const SAMPLE_MAX_BYTES = 20 * 1024 * 1024;

// 面板本地态：登记列表 + 功能是否可用（后端 SV 模型未加载则整体禁用）
let enabled = true;
let people = [];         // [{ id, name, unit, title, sample_count }]
let editingId = null;   // 当前正在行内编辑的人物 id（null=无）

function setStatus(text, kind) {
  const el = vpPop.querySelector('#vpStatus');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'vp-status' + (kind ? ' ' + kind : '');
}

async function loadList() {
  try {
    const res = await fetch('/voiceprints');
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    enabled = data.enabled !== false;
    people = Array.isArray(data.people) ? data.people : [];
  } catch (e) {
    enabled = false;
    people = [];
  }
}

function personRow(p) {
  const meta = [p.unit, p.title].map(s => (s || '').trim()).filter(Boolean).join(' · ');
  const isEditing = editingId === p.id;
  let body = '';
  if (isEditing) {
    body = `
      <div class="vp-edit-form">
        <div class="vp-edit-row">
          <label class="vp-edit-label">姓名</label>
          <input class="vp-inp vp-edit-inp" id="vpEditName" value="${escapeAttr(p.name)}" placeholder="姓名" autocomplete="off">
        </div>
        <div class="vp-edit-row">
          <label class="vp-edit-label">单位</label>
          <input class="vp-inp vp-edit-inp" id="vpEditUnit" value="${escapeAttr(p.unit || '')}" placeholder="单位（可选）" autocomplete="off">
        </div>
        <div class="vp-edit-row">
          <label class="vp-edit-label">职务</label>
          <input class="vp-inp vp-edit-inp" id="vpEditTitle" value="${escapeAttr(p.title || '')}" placeholder="职务（可选）" autocomplete="off">
        </div>
        <div class="vp-edit-ops">
          <button class="vp-mini" data-act="save-edit" data-id="${p.id}">保存</button>
          <button class="vp-mini" data-act="cancel-edit" data-id="${p.id}">取消</button>
        </div>
      </div>`;
  }
  return `
    <div class="vp-person" data-id="${p.id}">
      <div class="vp-person-head">
        <div class="vp-person-name">
          ${escapeHtml(p.name || '(未命名)')}
          <span class="vp-count">${p.sample_count || 0} 段样本</span>
        </div>
        ${meta && !isEditing ? `<div class="vp-person-meta">${escapeHtml(meta)}</div>` : ''}
      </div>
      ${isEditing ? body : `
      <div class="vp-person-ops">
        <button class="vp-mini" data-act="add-sample" data-id="${p.id}">＋样本</button>
        <button class="vp-mini" data-act="edit" data-id="${p.id}">编辑</button>
        <button class="vp-mini danger" data-act="delete" data-id="${p.id}">删除</button>
      </div>`}
    </div>`;
}

function render() {
  if (!enabled) {
    vpPop.innerHTML = `
      <div class="pop-title">声纹库</div>
      <p class="vp-hint">声纹功能未启用（后端未加载 SV 模型或已通过 SV_ENABLED 关闭）。</p>`;
    return;
  }
  const list = people.length
    ? people.map(personRow).join('')
    : '<div class="pop-sub">暂无声纹，先在下方登记第一个人</div>';
  vpPop.innerHTML = `
    <div class="pop-title">声纹库</div>
    <p class="vp-hint">上传每人一段清晰语音（≥3 秒为佳），登记后开会自动识别发言人。<br>音频仅用于提取声纹向量，提取后即丢弃、不留存。</p>
    <div class="vp-list">${list}</div>
    <div class="vp-enroll">
      <div class="vp-enroll-title">登记新声纹</div>
      <input class="vp-inp" id="vpName" placeholder="姓名（必填），如 张三">
      <div class="vp-enroll-row">
        <input class="vp-inp" id="vpUnit" placeholder="单位（可选）">
        <input class="vp-inp" id="vpTitle" placeholder="职务（可选）">
      </div>
      <div class="vp-enroll-row">
        <label class="vp-file-btn" for="vpFile">${icon('folder', 15)} 选择音频</label>
        <input type="file" id="vpFile" accept="audio/*" hidden>
        <span class="vp-file-name" id="vpFileName">未选择文件</span>
        <button class="btn-download" id="vpEnroll">登记</button>
      </div>
    </div>
    <div class="vp-status" id="vpStatus"></div>`;
  wire();
}

function wire() {
  // 每人操作按钮
  vpPop.querySelectorAll('.vp-person-ops [data-act]').forEach(btn => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); onPersonAct(btn.dataset.act, Number(btn.dataset.id)); });
  });
  // 行内编辑按钮（保存/取消）
  vpPop.querySelectorAll('.vp-edit-ops [data-act]').forEach(btn => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); onPersonAct(btn.dataset.act, Number(btn.dataset.id)); });
  });
  // 登记
  vpPop.querySelector('#vpEnroll')?.addEventListener('click', enroll);
  // 选文件后回显文件名（原生文件控件已隐藏，用自定义按钮 + 文件名代替）
  const fileEl = vpPop.querySelector('#vpFile');
  const nameEl = vpPop.querySelector('#vpFileName');
  fileEl?.addEventListener('change', () => {
    const f = fileEl.files && fileEl.files[0];
    if (nameEl) nameEl.textContent = f ? f.name : '未选择文件';
  });
}

async function onPersonAct(act, id) {
  if (act === 'edit') {
    editingId = id;
    render();
    // 聚焦姓名输入框
    const inp = vpPop.querySelector('#vpEditName');
    if (inp) inp.focus();
  } else if (act === 'save-edit') {
    await saveEdit(id);
  } else if (act === 'cancel-edit') {
    editingId = null;
    render();
  } else if (act === 'add-sample') {
    addSample(id);
  } else if (act === 'delete') {
    deletePerson(id);
  }
}

// —— 登记：姓名 + 单位/职务 + 文件 ——
async function enroll() {
  const name = vpPop.querySelector('#vpName').value.trim();
  const unit = vpPop.querySelector('#vpUnit').value.trim();
  const title = vpPop.querySelector('#vpTitle').value.trim();
  const fileEl = vpPop.querySelector('#vpFile');
  const file = fileEl.files && fileEl.files[0];
  if (!name) { setStatus('请填写姓名', 'err'); return; }
  if (!file) { setStatus('请选择语音文件', 'err'); return; }
  if (file.size > SAMPLE_MAX_BYTES) { setStatus('音频文件过大（上限 20MB）', 'err'); return; }

  const fd = new FormData();
  fd.append('name', name);
  fd.append('unit', unit);
  fd.append('title', title);
  fd.append('file', file);
  await submit(fd, '/voiceprints', 'POST', '已登记');
}

// —— 追加样本：给已有人物上传新的语音，强化平均声纹 ——
function addSample(personId) {
  pickFile(async (file) => {
    if (file.size > SAMPLE_MAX_BYTES) { setStatus('音频文件过大（上限 20MB）', 'err'); return; }
    const fd = new FormData();
    fd.append('file', file);
    await submit(fd, `/voiceprints/${personId}/samples`, 'POST', '已追加样本');
  });
}

async function saveEdit(id) {
  const nameEl = vpPop.querySelector('#vpEditName');
  const unitEl = vpPop.querySelector('#vpEditUnit');
  const titleEl = vpPop.querySelector('#vpEditTitle');
  const name = nameEl ? nameEl.value.trim() : '';
  if (!name) { setStatus('姓名不能为空', 'err'); return; }
  const unit = unitEl ? unitEl.value.trim() : '';
  const title = titleEl ? titleEl.value.trim() : '';
  await sendJson(`/voiceprints/${id}`, 'PATCH',
    { name, unit, title }, '已更新');
  editingId = null;
  render();
}

async function deletePerson(id) {
  const p = people.find(x => x.id === id);
  const ok = await confirmDialog({
    title: '删除声纹',
    message: `删除声纹「${p ? p.name : id}」？此操作不可撤销。`,
    confirmText: '删除',
    danger: true,
  });
  if (!ok) return;
  await sendJson(`/voiceprints/${id}`, 'DELETE', null, '已删除');
}

// —— 统一提交（multipart）：提交后刷新列表 ——
async function submit(fd, url, method, okMsg) {
  setStatus('处理中…', '');
  const enrollBtn = vpPop.querySelector('#vpEnroll');
  if (enrollBtn) enrollBtn.disabled = true;
  try {
    const res = await fetch(url, { method, body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    await loadList();
    render();
    setStatus(okMsg, 'ok');
  } catch (e) {
    setStatus('失败：' + (e.message || '网络错误'), 'err');
    if (enrollBtn) enrollBtn.disabled = false;
  }
}

// —— 统一提交（JSON / 无体）：编辑、删除 ——
async function sendJson(url, method, body, okMsg) {
  setStatus('处理中…', '');
  try {
    const opts = { method };
    if (body != null) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    await loadList();
    render();
    setStatus(okMsg, 'ok');
  } catch (e) {
    setStatus('失败：' + (e.message || '网络错误'), 'err');
  }
}

// 临时 <input type=file> 触发一次选择，回调拿到文件
function pickFile(cb) {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = 'audio/*';
  inp.addEventListener('change', () => {
    const f = inp.files && inp.files[0];
    if (f) cb(f);
  });
  inp.click();
}

export function initVoiceprints() {
  const vpBtn = $('vpBtn');
  if (!vpBtn) return;
  vpBtn.addEventListener('click', async () => {
    if (vpPop.style.display === 'block') { vpPop.style.display = 'none'; return; }
    await loadList();
    render();
    const r = vpBtn.getBoundingClientRect();
    vpPop.style.display = 'block';
    vpPop.style.top = (r.bottom + 6) + 'px';
    vpPop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 360)) + 'px';
  });
  // 点击面板外部关闭
  document.addEventListener('click', (e) => {
    if (vpPop.style.display !== 'block') return;
    if (vpPop.contains(e.target) || vpBtn.contains(e.target)) return;
    vpPop.style.display = 'none';
  });
}
