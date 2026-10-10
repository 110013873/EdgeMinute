// ============================================================
// 历史记录 I/O：payload 构建 + 文件结构持久化(PUT) + 音频落盘(POST)
// ------------------------------------------------------------
// 单一 payload 构建来源，供 autosave（编辑防抖 PUT）与 upload（添加文件即
// 落盘）共用，避免两处各写一份 fileToPayload 而漂移。
// ============================================================
import { state } from './state.js';

const SCHEMA_VERSION = 2;

// state.files[] → 历史 payload.files[]（纯数据，剔除 File/URL 等运行期字段）。
// 保留 status：草稿/转写中/失败文件也要如实回写，PUT 不得把它们抹成完成态。
// 保留 deleted：删除采用「原地留墓碑」而非数组重排（file_index 同时是下标/路由参数/
// 音频文件名前缀，见 CLAUDE.md），墓碑必须能被回写，否则服务端归一化边界会把这个
// 空槽当成一张普通 pending 卡片，删掉的录音在下一次自动保存时复活。
export function fileToPayload(f) {
  return {
    id: f.id,
    name: (f.file && f.file.name) || f.name || '',
    duration: f.duration,
    elapsed: f.elapsed,
    status: f.status || 'pending',
    error: f.error || '',
    audio_name: f.audio_name || '',
    has_audio: !!f.has_audio,
    deleted: !!f.deleted,
    segments: (f.segments || []).map(s => {
      const seg = {
        start: s.start, end: s.end, speaker: s.speaker,
        text: s.text, flagged: !!s.flagged,
      };
      // 行级发言人覆盖：服务端白名单只认这个键，不带上就会在下次自动保存后丢失
      const ov = (s.speakerName || '').trim();
      if (ov) seg.speakerName = ov;
      return seg;
    }),
  };
}

export function buildPayload() {
  return {
    schema_version: SCHEMA_VERSION,
    meta: state.meta,
    speakerMap: state.speakerMap,
    summary: state.summary || {},
    files: state.files.map(fileToPayload),
  };
}

// 固化当前 files/meta 结构到既有记录（PUT JSON）。未建档则跳过。
export async function persistFiles() {
  const id = state.currentMeetingId;
  if (id == null) return;
  const res = await fetch(`/history/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ payload: buildPayload() }),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
}

// 上传单个文件的音频到指定索引槽，返回服务端存储的 audio_name。
export async function uploadAudio(meetingId, fileIndex, file) {
  if (!file || !file.size) throw new Error('无有效音频');
  const fd = new FormData();
  fd.append('audio', file, file.name || `audio_${fileIndex}`);
  const res = await fetch(`/history/${meetingId}/audio/${fileIndex}`, { method: 'POST', body: fd });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data.audio_name || '';
}
