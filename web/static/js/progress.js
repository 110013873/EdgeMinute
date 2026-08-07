// ============================================================
// 转写进度条（假进度）：无法从 FunASR 拿到真实 ASR 进度，故按
//   预估耗时 = 音频时长 / 自适应处理倍速
// 把「已过时间」换算成百分比。设计要点：
//  - 进度是纯时间函数（processing 起点 + 预估总时长），与 renderFileList
//    的全量重建解耦：卡片 DOM 被重建也不丢进度，rAF 每帧按 id 重新定位
//    .prog-fill 改宽度即可。
//  - 封顶 90%，避免「跑满却还没完成」的尴尬；收到 done 由 finish() 补满 100%。
//  - 完成后用真实 (duration/elapsed) 滚动平均校准 state.asrSpeed，越用越准。
// 本模块不触碰任何转写/状态机逻辑，只读 state.files 的 status/duration。
// ============================================================
import { state } from './state.js';

// 每个文件的进度运行态：{ [fileId]: { startAt, dur, done, doneAt } }
// startAt/doneAt 用 performance.now()（单调时钟，不受系统时间调整影响）。
const runs = {};
let rafId = null;

const CAP = 0.9;              // 处理中封顶（未收到 done 前最多到 90%）
const FINISH_MS = 420;        // done 后从当前值补满 100% 的过渡时长
const SPEED_ALPHA = 0.3;      // 倍速滚动平均权重（新样本占比）
const SPEED_MIN = 3, SPEED_MAX = 60;  // 校准后倍速的合理区间

// 处理开始：记录起点与时长快照。duration 缺失时用兜底值（估算靠 renderProgress 兜底）。
// startedAt（可选，服务端墙钟秒 epoch）：处理的真实起始时刻。传入时把 rAF 用的
// 单调起点回算到那一刻，从而刷新/重连后进度按真实已过时间续上，而非从 0 重爬。
export function startProgress(item, startedAt) {
  if (!item) return;
  let startAt = performance.now();
  if (startedAt) {
    const elapsedMs = (Date.now() / 1000 - startedAt) * 1000;
    if (elapsedMs > 0) startAt = performance.now() - elapsedMs;  // 回拨到真实起点
  }
  runs[item.id] = { startAt, dur: item.duration || null, done: false, doneAt: 0 };
  ensureRaf();
}

// 切换会议：清空全部运行态。fileId 跨会议不唯一，若不清会误命中新会议同 id 的卡片。
export function clearAllProgress() {
  for (const k of Object.keys(runs)) delete runs[k];
  if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; }
}

// 处理结束（done/error）：done=true → 补满淡出；否则直接清除。
export function finishProgress(item, ok = true) {
  const r = runs[item && item.id];
  if (!r) return;
  if (ok) { r.done = true; r.doneAt = performance.now(); ensureRaf(); }
  else delete runs[item.id];
}

// 用真实耗时校准自适应倍速（仅在 duration/elapsed 都可信时）
export function calibrateSpeed(item) {
  const dur = item && item.duration, el = item && item.elapsed;
  if (!dur || !el || el <= 0) return;
  const observed = dur / el;
  if (!isFinite(observed) || observed <= 0) return;
  const next = state.asrSpeed * (1 - SPEED_ALPHA) + observed * SPEED_ALPHA;
  state.asrSpeed = Math.min(SPEED_MAX, Math.max(SPEED_MIN, next));
}

// 当前进度值 [0,1]；无运行态返回 null（不显示进度条）
function progressOf(item) {
  const r = runs[item.id];
  if (!r) return null;
  const now = performance.now();
  if (r.done) {
    const t = Math.min(1, (now - r.doneAt) / FINISH_MS);
    return CAP + (1 - CAP) * t;   // 从封顶值补满到 1
  }
  const dur = r.dur || item.duration || 0;
  const speed = state.asrSpeed > 0 ? state.asrSpeed : 15;
  const estMs = dur > 0 ? (dur / speed) * 1000 : 0;
  if (estMs <= 0) return 0.05;    // 时长未知：给一点点起步意思，靠 done 补满
  return Math.min(CAP, (now - r.startAt) / estMs);
}

function ensureRaf() {
  if (rafId == null) rafId = requestAnimationFrame(tick);
}

function tick() {
  rafId = null;
  let active = 0;
  const now = performance.now();
  for (const item of state.files) {
    const r = runs[item.id];
    if (!r) continue;
    const fill = document.querySelector(`.file-item[data-id="${item.id}"] .prog-fill`);
    const p = progressOf(item);
    if (fill && p != null) fill.style.width = (p * 100).toFixed(1) + '%';
    // done 补满淡出后清除运行态
    if (r.done && now - r.doneAt >= FINISH_MS) { delete runs[item.id]; continue; }
    active++;
  }
  if (active) rafId = requestAnimationFrame(tick);
}

// renderFileList 调用：某文件是否应渲染进度条 + 初始宽度（供重建后立即回填，避免闪 0）
export function progressState(item) {
  const r = runs[item.id];
  if (!r) return null;
  const p = progressOf(item);
  return { width: ((p ?? 0) * 100).toFixed(1) + '%' };
}
