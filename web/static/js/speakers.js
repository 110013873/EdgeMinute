// ============================================================
// 发言人：原始发言人集合、配色、显示名映射、筛选下拉
// ============================================================
import { state, speakerColors, nameColors, COLOR_PALETTE } from './state.js';
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
  // 簇级配色（原始发言人 → 颜色）：映射面板的色块用它，与显示名无关
  for (const sp of allOriginalSpeakers())
    if (!speakerColors[sp]) speakerColors[sp] = COLOR_PALETTE[state.colorSeq++ % COLOR_PALETTE.length];
  // 显示名配色：先让「与某簇当前显示名相同」的名字继承该簇颜色（与映射面板一致），
  // 再给行级覆盖产生的新名字分配一个颜色。
  for (const sp of allOriginalSpeakers()) {
    const n = speakerLabel(sp);
    if (!nameColors[n]) nameColors[n] = speakerColors[sp];
  }
  for (const item of state.files)
    for (const seg of item.segments) {
      const n = segmentLabel(seg);
      if (!nameColors[n]) nameColors[n] = COLOR_PALETTE[state.colorSeq++ % COLOR_PALETTE.length];
    }
}

export function speakerLabel(raw) {
  const key = String(raw);
  const m = state.speakerMap[key];
  if (m && m.trim()) return m.trim();
  return /^\d+$/.test(key) ? '说话人'+key : key;
}

// 单段的显示名，三层解析（与后端 services.resolve_segment_speaker 一一对应）：
//   1. seg.speakerName —— 行级覆盖（列表里对单行的手动修正）
//   2. speakerMap[seg.speaker] —— 全局「映射发言人」（一个簇整场统一改名）
//   3. 默认 —— "说话人N" / 原样
// 两处必须同步改：否则界面与导出/问答会对同一段给出不同的名字。
export function segmentLabel(seg) {
  const override = ((seg && seg.speakerName) || '').trim();
  if (override) return override;
  return speakerLabel((seg && seg.speaker) ?? '?');
}

export function speakerColor(raw) { return speakerColors[String(raw)] || 'var(--accent)'; }

// 显示名 → 颜色。发言人的视觉身份跟随**显示名**而不是原始簇：把某行从"说话人0"
// 改成"李四"后，若仍沿用簇颜色，这一行与张三的行还是同一个颜色，视觉上等于没拆开。
export function speakerNameColor(name) {
  const key = (name || '').trim() || '?';
  if (!nameColors[key]) nameColors[key] = COLOR_PALETTE[state.colorSeq++ % COLOR_PALETTE.length];
  return nameColors[key];
}

// 行级覆盖：把这（合并行覆盖的多）段的显示名固定为 name，只写 seg.speakerName。
// 绝不改写 seg.speaker（合并/配色/筛选/声纹标注都以它为簇键），也绝不碰 speakerMap——
// 批量映射由「映射发言人」面板负责，两者互不干扰。
// 与当前显示名相同则跳过，避免写入冗余覆盖（那会让该段脱离后续的簇映射）。
export function setRowSpeaker(item, idxs, name) {
  const v = (name || '').trim();
  let changed = false;
  for (const i of idxs) {
    const seg = item.segments[i];
    if (!seg || segmentLabel(seg) === v) continue;
    if (v) seg.speakerName = v; else delete seg.speakerName;
    changed = true;
  }
  return changed;
}

// 清除全部行级覆盖（「全部还原」用）。
export function clearRowSpeakers() {
  for (const item of state.files) for (const seg of item.segments) delete seg.speakerName;
}

// 就地重命名：把“当前显示名等于 oldLabel”的所有原始发言人一并映射为 newLabel。
// 走 speakerMap（绝不改写 seg.speaker），因此界面上凡是显示为 oldLabel 的段都会同步变。
// 被改动的行清除其声纹“自动识别”徽标记录。返回是否有变更（调用方负责 pushUndo/重绘）。
export function renameSpeakerLabel(oldLabel, newLabel) {
  const from = (oldLabel || '').trim();
  const to = (newLabel || '').trim();
  if (!to || from === to) return false;
  const targets = allOriginalSpeakers().filter(sp => speakerLabel(sp) === from);
  if (!targets.length) return false;
  for (const sp of targets) {
    state.speakerMap[sp] = to;
    delete state.speakerAuto[sp];   // 用户手动重命名 → 不再是自动识别结果
  }
  refreshSpeakerFilter();
  return true;
}

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
