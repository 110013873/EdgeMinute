// ============================================================
// 导出：会议纪要（后端模板渲染）/ 文字发言稿（内联零依赖 OpenXML + STORE zip）
// ============================================================
import { state } from './state.js';
import { $, downloadBtn } from './dom.js';
import { xmlEsc, formatTime, joinText } from './util.js';
import { speakerLabel } from './speakers.js';
import { attendeeLines, hasMeta, displayDate } from './meta-panel.js';
import { ensureSummary } from './summary.js';

function collectSegments(item) {
  const merge = $('optMergeSpk').checked;
  if (!merge) return item.segments.slice();
  const out = [];
  for (const seg of item.segments) {
    const last = out[out.length-1];
    if (last && String(last.speaker) === String(seg.speaker))
      out[out.length-1] = { ...last, end: seg.end, text: joinText(last.text, seg.text) };
    else out.push({ ...seg, text: seg.text.trim() });
  }
  return out;
}

// ---------- 文字发言稿 DOCX（内联零依赖 OpenXML + zip STORE） ----------
// 生成一个 <w:p> 段落；opts: {bold, size(半磅), color, align}
function docxPara(text, opts = {}) {
  const runs = String(text).split('\n').map((line, i) => {
    const brk = i > 0 ? '<w:br/>' : '';
    const rpr = ['<w:rFonts w:ascii="宋体" w:eastAsia="宋体" w:hAnsi="宋体"/>'];
    if (opts.bold) rpr.push('<w:b/>');
    if (opts.size) rpr.push(`<w:sz w:val="${opts.size}"/>`);
    if (opts.color) rpr.push(`<w:color w:val="${opts.color}"/>`);
    const rprXml = `<w:rPr>${rpr.join('')}</w:rPr>`;
    return `<w:r>${rprXml}${brk}<w:t xml:space="preserve">${xmlEsc(line)}</w:t></w:r>`;
  }).join('');
  const ppr = ['<w:ind w:firstLine="0" w:firstLineChars="0" w:left="0"/>'];
  if (opts.align) ppr.push(`<w:jc w:val="${opts.align}"/>`);
  if (opts.spaceAfter) ppr.push(`<w:spacing w:after="${opts.spaceAfter}"/>`);
  const pprXml = `<w:pPr>${ppr.join('')}</w:pPr>`;
  return `<w:p>${pprXml}${runs}</w:p>`;
}

// 文字发言稿正文：会议信息抬头（可选）+ 逐段发言记录。复用 ⚙ 导出选项。
function buildTranscriptXml() {
  const ts = $('optTimestamp').checked;
  const showSpk = $('optSpeaker').checked;
  const useMeta = $('optMeta').checked && hasMeta();
  const withResults = state.files.filter(f => f.segments.length > 0);
  const meta = state.meta;
  let body = '';
  if (useMeta) {
    body += docxPara(meta.title || '会议发言稿', { bold:true, size:36, align:'center', spaceAfter:180 });
    if (meta.date) body += docxPara(`时间：${displayDate()}`, { size:21, spaceAfter:60 });
    if (meta.place) body += docxPara(`地点：${meta.place}`, { size:21, spaceAfter:60 });
    const atts = attendeeLines();
    if (atts.length) body += docxPara(`参会人员：${atts.join('；')}`, { size:21, spaceAfter:60 });
    if (meta.agenda) body += docxPara(`议程：${meta.agenda}`, { size:21, spaceAfter:160 });
    body += docxPara('会议记录', { bold:true, size:26, spaceAfter:120 });
  }
  for (const item of withResults) {
    if (withResults.length > 1) body += docxPara(item.file.name, { bold:true, size:24, spaceAfter:80 });
    for (const seg of collectSegments(item)) {
      const tm = ts ? `[${formatTime(seg.start)}-${formatTime(seg.end)}] ` : '';
      const spk = showSpk ? `${speakerLabel(seg.speaker)}：` : '';
      body += docxPara(`${tm}${spk}${seg.text}`, { size:22, spaceAfter:60 });
    }
  }
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>${body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr></w:body>
</w:document>`;
}

// 最小 zip（STORE，无压缩）打包器，内联实现，浏览器原生
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(bytes) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}
function makeZip(fileMap) {
  const enc = new TextEncoder();
  let offset = 0;
  const chunks = [];
  const central = [];
  const u16 = (n) => [n & 0xFF, (n >>> 8) & 0xFF];
  const u32 = (n) => [n & 0xFF, (n >>> 8) & 0xFF, (n >>> 16) & 0xFF, (n >>> 24) & 0xFF];

  for (const [name, str] of Object.entries(fileMap)) {
    const nameBytes = enc.encode(name);
    const data = enc.encode(str);
    const crc = crc32(data);
    const local = [].concat(
      u32(0x04034b50), u16(20), u16(0), u16(0), u16(0), u16(0),
      u32(crc), u32(data.length), u32(data.length),
      u16(nameBytes.length), u16(0)
    );
    const localHeader = new Uint8Array(local);
    chunks.push(localHeader, nameBytes, data);
    const localSize = localHeader.length + nameBytes.length + data.length;
    central.push({ name: nameBytes, crc, size: data.length, offset });
    offset += localSize;
  }
  const centralChunks = [];
  let centralSize = 0;
  for (const c of central) {
    const cd = [].concat(
      u32(0x02014b50), u16(20), u16(20), u16(0), u16(0), u16(0), u16(0),
      u32(c.crc), u32(c.size), u32(c.size),
      u16(c.name.length), u16(0), u16(0), u16(0), u16(0), u32(0), u32(c.offset)
    );
    const cdHeader = new Uint8Array(cd);
    centralChunks.push(cdHeader, c.name);
    centralSize += cdHeader.length + c.name.length;
  }
  const end = new Uint8Array([].concat(
    u32(0x06054b50), u16(0), u16(0), u16(central.length), u16(central.length),
    u32(centralSize), u32(offset), u16(0)
  ));
  const all = [...chunks, ...centralChunks, end];
  let total = 0; for (const a of all) total += a.length;
  const out = new Uint8Array(total);
  let p = 0; for (const a of all) { out.set(a, p); p += a.length; }
  return out;
}
function buildTranscriptBlob() {
  const parts = {
    '[Content_Types].xml': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>`,
    '_rels/.rels': `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>`,
    'word/document.xml': buildTranscriptXml(),
  };
  const zip = makeZip(parts);
  return new Blob([zip], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' });
}

// ---------- 导出触发 ----------
function triggerDownload(blob, ext) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = ((state.meta.title || 'transcript').replace(/[\\/:*?"<>|]/g,'_')) + '.' + ext;
  a.click();
  URL.revokeObjectURL(url);
}

// 文字发言稿：纯前端生成 .docx（完整转写发言记录 + 可选会议抬头）
function exportTranscript() {
  const withResults = state.files.filter(f => f.segments.length > 0);
  if (!withResults.length) { alert('没有可导出的转写内容'); return; }
  triggerDownload(buildTranscriptBlob(), 'docx');
}

// 会议纪要：把当前数据发给后端，用 templates/minutes.docx 渲染
async function exportViaTemplate() {
  const withResults = state.files.filter(f => f.segments.length > 0);
  // 汇总所有文件的段落（多文件时顺序拼接）
  const segments = [];
  for (const item of withResults) for (const seg of item.segments) segments.push(seg);
  const options = {
    mergeSpeaker: $('optMergeSpk').checked,
    timestamp: $('optTimestamp').checked,
    speaker: $('optSpeaker').checked,
    speakerMap: state.speakerMap,
  };
  downloadBtn.disabled = true;
  const prev = downloadBtn.textContent;
  try {
    // 会议总结（概要/各发言人观点/决议）：已生成则复用，未生成则自动调用接口生成后再导出。
    // 生成失败不阻断导出——退化为对应段落留空，保证仍能拿到 Word。
    let summaryPayload = null;
    try {
      downloadBtn.textContent = state.summary ? '生成中…' : '总结中…';
      const summary = await ensureSummary();
      if (summary) {
        summaryPayload = {
          overview: summary.overview || '',
          speakers: summary.speakers || [],
          resolutions: Array.isArray(summary.resolutions) ? summary.resolutions : (summary.resolutions || []),
        };
      }
    } catch (e) {
      // 总结失败仅记录，继续导出（概要/观点/决议为空）
    }
    downloadBtn.textContent = '生成中…';
    const res = await fetch('/export/docx-template', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meta: state.meta, segments, options, summary: summaryPayload }),
    });
    if (!res.ok) {
      let msg = `导出失败（${res.status}）`;
      try { const j = await res.json(); if (j.error) msg = '导出失败：' + j.error; } catch (e) {}
      alert(msg);
      return;
    }
    triggerDownload(await res.blob(), 'docx');
  } catch (e) {
    alert('导出失败：网络错误');
  } finally {
    downloadBtn.textContent = prev;
    downloadBtn.disabled = false;
  }
}

// 事件绑定（在 main.js 装配阶段调用一次）
export function initExport() {
  const exportOptsBtn = $('exportOptsBtn');
  const exportOptsPop = $('exportOptsPop');
  const exportMenuPop = $('exportMenuPop');
  const spkPop = $('spkPop');

  const closeExportMenu = () => { exportMenuPop.style.display = 'none'; };

  exportOptsBtn.addEventListener('click', () => {
    if (exportOptsPop.style.display === 'block') { exportOptsPop.style.display = 'none'; return; }
    const r = exportOptsBtn.getBoundingClientRect();
    exportOptsPop.style.display = 'block';
    exportOptsPop.style.top = (r.bottom + 6) + 'px';
    exportOptsPop.style.left = Math.max(8, r.right - 220) + 'px';
  });

  // 导出按钮：打开/关闭导出方式菜单
  downloadBtn.addEventListener('click', () => {
    if (exportMenuPop.style.display === 'block') { closeExportMenu(); return; }
    const r = downloadBtn.getBoundingClientRect();
    exportMenuPop.style.display = 'block';
    exportMenuPop.style.top = (r.bottom + 6) + 'px';
    exportMenuPop.style.left = Math.max(8, r.right - 200) + 'px';
  });

  $('exportMinutesItem').addEventListener('click', () => { closeExportMenu(); exportViaTemplate(); });
  $('exportTranscriptItem').addEventListener('click', () => { closeExportMenu(); exportTranscript(); });

  document.addEventListener('click', (e) => {
    if (exportOptsPop.style.display === 'block' && !exportOptsPop.contains(e.target) && e.target !== exportOptsBtn) exportOptsPop.style.display = 'none';
    if (exportMenuPop.style.display === 'block' && !exportMenuPop.contains(e.target) && e.target !== downloadBtn) closeExportMenu();
    if (spkPop.style.display === 'block' && !spkPop.contains(e.target) && e.target !== $('spkBtn')) spkPop.style.display = 'none';
  });
}
