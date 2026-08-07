// ============================================================
// 发言人映射弹出面板（映射发言人按钮）
// ============================================================
import { state } from './state.js';
import { $ } from './dom.js';
import { escapeHtml, escapeAttr, cssEsc } from './util.js';
import { allOriginalSpeakers, speakerColor, refreshSpeakerFilter } from './speakers.js';
import { attendeeList } from './meta-panel.js';
import { renderResults } from './segments.js';
import { pushUndo } from './undo.js';
import { scheduleAutoSave } from './autosave.js';
import { icon } from './icons.js';

const spkPop = $('spkPop');

export function renderSpkPop() {
  const speakers = allOriginalSpeakers();
  const attendees = attendeeList();
  const rows = speakers.map(sp => {
    const cur = state.speakerMap[sp] || '';
    const auto = state.speakerAuto[sp];  // { name, score }（声纹自动识别，若有）
    // 仅当该行显示名仍等于自动识别填入的值时，才标注“自动识别”徽标；
    // 用户（在任何地方）把名字改成别的值后，cur !== auto.name，徽标自动消失。
    // 声纹自动识别标记：用紧凑 ICON 代替文字（避免长名字被徽标挤到换行），
    // 相似度放进 title 悬浮提示。
    const autoBadge = (auto && cur && cur === auto.name)
      ? `<span class="spk-auto" title="声纹自动识别，相似度 ${Number(auto.score).toFixed(2)}">${icon('mic', 13)}</span>`
      : '';
    const opts = attendees.length
      ? `<select data-pick="${escapeAttr(sp)}">
          <option value="">选择参会人…</option>
          ${attendees.map(a => `<option value="${escapeAttr(a)}"${a===cur?' selected':''}>${escapeHtml(a)}</option>`).join('')}
        </select>`
      : '';
    return `
    <div class="spk-row">
      <span class="swatch" style="background:${speakerColor(sp)}"></span>
      <span class="orig">${/^\d+$/.test(sp) ? '说话人'+sp : escapeHtml(sp)}${autoBadge}</span>
      ${opts}
      <input data-sp="${escapeAttr(sp)}" value="${escapeAttr(cur)}" placeholder="映射为，如 张三">
    </div>`;
  }).join('') || '<div class="pop-sub">转写后才会出现发言人</div>';
  spkPop.innerHTML = `
    <div class="pop-title">映射发言人</div>
    <div class="pop-sub">${attendees.length ? '可从参会人员下拉选择，或直接输入自定义名称' : '填写会议信息中的参会人员后，此处可下拉选择'}</div>
    ${rows}
    <div class="pop-actions">
      <button class="btn-download" id="spkApply">应用映射</button>
      <button class="btn-plain" id="spkReset">全部还原</button>
    </div>`;
  // 下拉选择时回填到同一行的输入框
  spkPop.querySelectorAll('[data-pick]').forEach(sel => {
    sel.addEventListener('change', () => {
      const inp = spkPop.querySelector(`input[data-sp="${cssEsc(sel.dataset.pick)}"]`);
      if (inp) inp.value = sel.value;
    });
  });
  spkPop.querySelector('#spkApply')?.addEventListener('click', () => {
    pushUndo();
    spkPop.querySelectorAll('input[data-sp]').forEach(inp => {
      const sp = inp.dataset.sp, v = inp.value.trim();
      const prev = (state.speakerMap[sp] || '').trim();
      if (v) state.speakerMap[sp] = v; else delete state.speakerMap[sp];
      // 用户手动改动该行 → 该值不再是“自动识别”结果，清除徽标记录
      if (v !== prev) delete state.speakerAuto[sp];
    });
    refreshSpeakerFilter();
    renderResults();
    spkPop.style.display = 'none';
    scheduleAutoSave();
  });
  spkPop.querySelector('#spkReset')?.addEventListener('click', () => {
    pushUndo();
    state.speakerMap = {};
    state.speakerAuto = {};  // 还原时一并清空自动识别记录
    refreshSpeakerFilter(); renderResults(); renderSpkPop();
    scheduleAutoSave();
  });
}

export function initSpkPop() {
  $('spkBtn').addEventListener('click', () => {
    if (spkPop.style.display === 'block') { spkPop.style.display = 'none'; return; }
    renderSpkPop();
    const r = $('spkBtn').getBoundingClientRect();
    spkPop.style.display = 'block';
    spkPop.style.top = (r.bottom + 6) + 'px';
    spkPop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 340)) + 'px';
  });
}
