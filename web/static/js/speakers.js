// ============================================================
// 发言人：原始发言人集合、配色、显示名映射、筛选下拉
// ============================================================
import { state, speakerColors, COLOR_PALETTE } from './state.js';
import { $ } from './dom.js';
import { escapeHtml, escapeAttr } from './util.js';

export function allOriginalSpeakers() {
  const set = new Set();
  for (const item of state.files) for (const seg of item.segments) set.add(String(seg.speaker));
  return [...set].sort((a,b) => {
    const na=/^\d+$/.test(a), nb=/^\d+$/.test(b);
    if (na && nb) return Number(a)-Number(b);
    return a.localeCompare(b);
  });
}

export function ensureSpeakerColors() {
  for (const sp of allOriginalSpeakers())
    if (!speakerColors[sp]) speakerColors[sp] = COLOR_PALETTE[state.colorSeq++ % COLOR_PALETTE.length];
}

export function speakerLabel(raw) {
  const key = String(raw);
  const m = state.speakerMap[key];
  if (m && m.trim()) return m.trim();
  return /^\d+$/.test(key) ? '说话人'+key : key;
}

export function speakerColor(raw) { return speakerColors[String(raw)] || 'var(--accent)'; }

// 静默应用声纹自动匹配结果 matches = { spk: {name, score} }。
// 只填用户尚未手动设定的簇（speakerMap 中该键缺失或为空），绝不覆盖已有手填值。
// 记录 { name, score } 到 state.speakerAuto 供面板标注——存 name 是为了让面板只在
// 显示名仍等于当初自动填入的值时才标“自动识别”，用户改成别的名字后徽标即失效。
// 返回是否有变更。
export function applySpeakerMatches(matches) {
  let changed = false;
  for (const [sp, info] of Object.entries(matches || {})) {
    if (!info || !info.name) continue;
    const key = String(sp);
    const existing = (state.speakerMap[key] || '').trim();
    if (existing) continue;            // 用户已手动映射：不覆盖
    state.speakerMap[key] = info.name;
    state.speakerAuto[key] = { name: info.name, score: info.score };
    changed = true;
  }
  return changed;
}

export function hexToDim(hex) {
  if (!hex.startsWith('#') || hex.length < 7) return 'var(--accent-dim)';
  const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},0.14)`;
}

// 依据当前所有原始发言人重建筛选下拉；保留当前选中项，选中项已不存在则回退到"全部"
export function refreshSpeakerFilter() {
  const sel = $('speakerFilter');
  if (!sel) return;
  const speakers = allOriginalSpeakers();
  if (state.speakerFilter && !speakers.includes(state.speakerFilter)) state.speakerFilter = '';
  sel.innerHTML = '<option value="">全部发言人</option>' +
    speakers.map(sp => `<option value="${escapeAttr(sp)}"${sp===state.speakerFilter?' selected':''}>${escapeHtml(speakerLabel(sp))}</option>`).join('');
  sel.value = state.speakerFilter;
}
