// ============================================================
// 转写行标注声纹：点击 ⚑ → 截取段音频提声纹 → 登记到声纹库
// ------------------------------------------------------------
// 弹出轻量选择面板：列出已登记人物（快速选择）或输入姓名新建。
// 后端接口：POST /history/{meeting_id}/audio/{file_index}/voiceprint
// ============================================================
import { state } from './state.js';
import { escapeHtml } from './util.js';
import { scheduleAutoSave } from './autosave.js';

let popover = null;
let pendingItem = null;   // 当前标注对应的 file item
let pendingSeg = null;    // 当前标注对应的 segment
let pendingBtn = null;    // 触发按钮（用于定位）

function ensurePopover() {
  if (popover) return;
  popover = document.createElement('div');
  popover.id = 'vpAnnotatePop';
  popover.className = 'popover';
  popover.style.display = 'none';
  popover.style.minWidth = '260px';
  document.body.appendChild(popover);
}

async function loadPeople() {
  try {
    const res = await fetch('/voiceprints');
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return { enabled: data.enabled !== false, people: data.people || [] };
  } catch (e) {
    return { enabled: false, people: [], error: e.message };
  }
}

function render(people, enabled) {
  if (!enabled) {
    popover.innerHTML = `
      <div class="pop-title">标注声纹</div>
      <p class="vp-hint" style="padding:8px 0;">声纹功能未启用，无法标注。</p>`;
    return;
  }
  const seg = pendingSeg;
  const spk = seg ? String(seg.speaker) : '?';
  popover.innerHTML = `
    <div class="pop-title">标注声纹 — 说话人${escapeHtml(spk)}</div>
    <div class="vp-annot-body">
      ${people.length ? `
        <div class="va-section">
          <div class="va-label">选择已登记人物</div>
          <div class="va-people">${people.map(p => `
            <button class="va-person-btn" data-id="${p.id}" data-name="${escapeHtml(p.name)}">
              ${escapeHtml(p.name)}
              <span class="va-meta">${[p.unit, p.title].map(s => (s || '').trim()).filter(Boolean).join(' · ') || '—'}</span>
              <span class="va-count">${p.sample_count} 段样本</span>
            </button>`).join('')}
          </div>
        </div>
      ` : '<div class="va-section"><div class="va-label">暂未登记任何人物</div></div>'}
      <div class="va-section">
        <div class="va-label">或输入姓名新建</div>
        <div class="va-new-row">
          <input class="va-inp" id="vaNameInput" placeholder="输入人物姓名" autocomplete="off">
          <button class="va-submit" id="vaSubmitBtn">登记</button>
        </div>
      </div>
    </div>
    <div class="vp-status" id="vaStatus"></div>`;

  // 绑定事件
  popover.querySelectorAll('.va-person-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name;
      if (name) submitAnnotation(name);
    });
  });
  const submitBtn = popover.querySelector('#vaSubmitBtn');
  const nameInput = popover.querySelector('#vaNameInput');
  if (submitBtn && nameInput) {
    submitBtn.addEventListener('click', () => {
      const name = nameInput.value.trim();
      if (!name) { setStatus('请输入姓名', 'err'); return; }
      submitAnnotation(name);
    });
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitBtn.click();
      }
    });
  }
}

function setStatus(text, kind) {
  const el = popover.querySelector('#vaStatus');
  if (!el) return;
  el.textContent = text || '';
  el.className = 'vp-status' + (kind ? ' ' + kind : '');
}

async function submitAnnotation(personName) {
  const item = pendingItem;
  const seg = pendingSeg;
  if (!item || !seg) return;
  const meetingId = state.currentMeetingId;
  if (meetingId == null) { setStatus('会议未建档', 'err'); return; }
  const fileIdx = state.files.indexOf(item);
  if (fileIdx < 0) { setStatus('文件未找到', 'err'); return; }

  setStatus('提取声纹中…', '');
  const submitBtn = popover.querySelector('#vaSubmitBtn');
  if (submitBtn) submitBtn.disabled = true;

  try {
    const res = await fetch(`/history/${meetingId}/audio/${fileIdx}/voiceprint`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start: seg.start,
        end: seg.end,
        person_name: personName,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));

    const label = data.is_new ? '已新建' : '已追加';
    setStatus(`${label}：${escapeHtml(data.person_name)}（共 ${data.sample_count} 段样本）`, 'ok');
    // 成功后延迟关闭面板
    setTimeout(() => closePopover(), 1500);
  } catch (e) {
    setStatus('失败：' + (e.message || '网络错误'), 'err');
    if (submitBtn) submitBtn.disabled = false;
  }
}

function positionPopover(anchorEl) {
  const r = anchorEl.getBoundingClientRect();
  popover.style.top = (r.bottom + 6) + 'px';
  popover.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 280)) + 'px';
}

function closePopover() {
  popover.style.display = 'none';
  pendingItem = null;
  pendingSeg = null;
  pendingBtn = null;
}

export async function openAnnotatePopover(item, seg, btnEl) {
  ensurePopover();
  // 如果已打开且是同一个按钮 → 关闭
  if (popover.style.display === 'block' && pendingBtn === btnEl) {
    closePopover();
    return;
  }
  pendingItem = item;
  pendingSeg = seg;
  pendingBtn = btnEl;
  popover.innerHTML = '<div class="pop-title">标注声纹</div><p class="vp-hint" style="padding:8px 0;">加载中…</p>';
  positionPopover(btnEl);
  popover.style.display = 'block';

  const { enabled, people } = await loadPeople();
  // 可能在加载期间关闭了面板或切换了按钮
  if (popover.style.display !== 'block' || pendingBtn !== btnEl) return;
  render(people, enabled);
  positionPopover(btnEl); // 重新定位（内容可能变化）
}

export function initAnnotatePopover() {
  ensurePopover();
  // 点击面板外部关闭
  document.addEventListener('click', (e) => {
    if (!popover || popover.style.display !== 'block') return;
    if (popover.contains(e.target)) return;
    if (pendingBtn && pendingBtn.contains(e.target)) return;
    closePopover();
  });
  // Esc 关闭
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && popover && popover.style.display === 'block') {
      closePopover();
    }
  });
}
