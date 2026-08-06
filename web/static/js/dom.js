// ============================================================
// DOM 访问：$ 取元素助手 + 常用元素缓存
// ------------------------------------------------------------
// 本模块在 import 时即读取元素（module 脚本为 defer，HTML 已解析完毕）。
// 仅缓存“页面结构固定、始终存在”的元素；动态生成的节点仍用 $ 现查。
// ============================================================
export const $ = (id) => document.getElementById(id);

export const dropzone = $('dropzone');
export const fileInput = $('fileInput');
export const fileListEl = $('fileList');
export const runAllBtn = $('runAllBtn');
export const resultsScroll = $('resultsScroll');
export const emptyHint = $('emptyHint');
export const downloadBtn = $('downloadBtn');
export const gAudio = $('globalAudio');
