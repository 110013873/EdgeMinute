// ============================================================
// 全局可变状态容器（单一对象，规避 ESM live-binding 无法跨模块重新赋值的限制）
// ------------------------------------------------------------
// 原本散落在顶层作用域、会被“整体重新赋值”的变量（files/meta/speakerMap/
// summary 等），统一收敛到本对象的属性上：跨模块写成 state.files = ... 是合法
// 的属性赋值，而 import { files } 得到的是只读绑定，赋值会抛错。
//
// 约定：
//  - 需要被“整体替换”的状态 → 挂在 state 上（state.files = state.files.filter(...)）
//  - 生命周期内只做“原地增删”的集合/常量 → 用 export const 直接导出（见下方）
// files: [{ id, file, url, status, segments:[{start,end,speaker,text,flagged}],
//           elapsed, duration }]
//   seg.speaker 始终保存 cam++ 原始值，永不改写；显示名走 speakerMap
// ============================================================
export const state = {
  files: [],
  fileSeq: 0,
  activeSegEl: null,
  playingSegEl: null,

  speakerMap: {},
  // 声纹自动识别记录：{ [原始spk]: { name, score } }。仅用于在映射面板标注
  // “自动识别(0.82)”，不参与显示名解析（显示名一律走 speakerMap）。存 name 是为了
  // 让徽标只在显示名仍等于自动填入值时出现——用户改名后 cur!==name，徽标自动失效。
  speakerAuto: {},
  colorSeq: 0,

  // attendees 为结构化数组 [{name,unit,title}]；host 字段已移除
  meta: { title: '', date: '', place: '', attendees: [], agenda: '' },

  // 播放（唯一音频源：底部 globalAudio）
  currentItem: null,       // 当前载入底部播放器的文件
  playbackRate: 1.0,
  loopSeg: null,

  // 搜索
  searchMatches: [],
  searchPos: -1,

  // 视图选项：按发言人筛选（原始 speaker 值，空=全部）
  speakerFilter: '',

  // 智能问答
  chatHistory: [],         // [{role:'user'|'assistant', content}]
  chatAbort: null,         // 进行中的请求 AbortController

  // 会议总结
  summary: null,           // { overview, speakers:[{speaker,points}], resolutions:[str] }
  summaryBusy: false,
  summaryColorSeq: 0,

  // 会议历史：当前工作区对应的历史记录 id（null=尚未保存过）
  currentMeetingId: null,

  // 转写进度条（假进度，无法拿到真实 ASR 进度）：自适应处理倍速，
  // 初值 15x，每次转写完成后用真实 (duration/elapsed) 做滚动平均校准。
  // 仅用于把「音频时长 → 预估耗时」换算成进度百分比，不影响任何转写逻辑。
  asrSpeed: 15,
};

// —— 生命周期内只做原地增删、无需整体替换的集合/常量 ——
export const speakerColors = {};
export const COLOR_PALETTE = ['#4f8cff','#3ecf8e','#f2c14e','#e879a6','#a78bfa','#38bdf8','#fb923c','#34d399','#f472b6','#c084fc'];

// 撤销/重做
export const undoStack = [];
export const redoStack = [];
export const UNDO_LIMIT = 60;

// 会议总结各板块展开态 + 发言人配色
export const summaryOpen = { overview: true, speakers: true, resolutions: true };
export const summarySpeakerColors = {};

// 归一化外来 meta（旧会话 / 导入结果 / 历史恢复）：兼容字符串型 attendees、剔除 host
export function normalizeMeta(raw) {
  raw = raw || {};
  let attendees = raw.attendees;
  if (typeof attendees === 'string') {
    attendees = attendees.split(/[、,，;；\n\r\t ]+/).map(s => s.trim()).filter(Boolean)
      .map(name => ({ name, unit:'', title:'' }));
  } else if (Array.isArray(attendees)) {
    attendees = attendees
      .filter(a => a && typeof a === 'object')
      .map(a => ({ name:(a.name||'').trim(), unit:(a.unit||'').trim(), title:(a.title||'').trim() }))
      .filter(a => a.name);
  } else {
    attendees = [];
  }
  return {
    title: (raw.title||'').trim(), date: (raw.date||''), place: (raw.place||'').trim(),
    agenda: (raw.agenda||''), attendees,
  };
}
