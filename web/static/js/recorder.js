// ============================================================
// 麦克风录音：设备选择 / 开始 / 停止 / 计时
// ------------------------------------------------------------
// 采集在**服务端**（ffmpeg + ALSA），不是浏览器 getUserMedia——浏览器多半从局域网
// 另一台机器访问 http://<host>:8899，非安全上下文下 getUserMedia 会被直接拒绝；
// 且麦克风物理插在服务端机器上。本模块只负责发指令与渲染，录音与浏览器解耦：
// 关掉页面录音照常继续，重开后由 GET /history/{id}/record 恢复按钮态与计时起点。
//
// 计时取自服务端（recorded_seconds），前端不自己数秒——否则刷新后计时归零、与实际
// 已录时长脱节。轮询只在「本端确实在录」时运行，切走即停。
//
// 录音期间不做任何推理（见 CLAUDE.md/方案）：停止后整文件转写一次，走的就是上传
// 录音的同一条路（POST /transcribe），说话人分离质量与上传同一份音频等价。
// ============================================================
import { state } from './state.js';
import { $ } from './dom.js';
import { toast, confirmDialog } from './ui-feedback.js';
import { renderFileList, updateRunAllState } from './upload.js';
import { formatTime } from './util.js';

// 计时轮询间隔：秒表看着连续即可，又不至于把 8899 打成压力测试。
const POLL_MS = 1000;

let devices = [];        // 服务端枚举到的采集设备
let available = false;   // 录音功能是否可用（RECORD_ENABLED + 探测到设备）
let pollTimer = null;
let barEl = null;        // 录音条根节点（惰性创建）
let recordingTagEl = null;  // 工具栏脉冲指示

// ---------- 服务端交互 ----------
async function fetchDevices() {
  try {
    const res = await fetch('/record/devices');
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    devices = data.devices || [];
    available = !!data.enabled;
    return data;
  } catch (e) {
    devices = [];
    available = false;
    return { devices: [], default_device: '' };
  }
}

async function fetchStatus() {
  const id = state.currentMeetingId;
  if (id == null) return { recording: false };
  try {
    const res = await fetch(`/history/${id}/record`);
    const data = await res.json();
    if (!res.ok || !data.ok) return { recording: false };
    return data;
  } catch (e) {
    return { recording: false };
  }
}

// ---------- 录音条构建 ----------
function buildBar() {
  const wrap = document.createElement('div');
  wrap.className = 'rec-bar';
  wrap.id = 'recBar';
  wrap.hidden = true;
  // 一行到底：设备下拉 + 开始/停止。早先这里还有一条电平条与一段说明文字，都已去掉
  // （用户要求精简）。计时行只在录制中出现——空闲时它恒为 00:00:00，纯占位。
  wrap.innerHTML = `
    <div class="rec-row">
      <span class="rec-dot" aria-hidden="true"></span>
      <select id="recDevice" class="rec-device" title="选择输入设备"></select>
      <button class="rec-btn" id="recToggle" type="button">开始录音</button>
    </div>
    <div class="rec-meta" id="recMeta" hidden>
      <span class="rec-time" id="recTime">00:00:00</span>
    </div>
  `;
  const dz = $('dropzone');
  dz.parentNode.insertBefore(wrap, dz.nextSibling);
  return wrap;
}

function renderDevices(defaultDevice) {
  const sel = $('recDevice');
  sel.innerHTML = '';
  for (const d of devices) {
    const opt = document.createElement('option');
    opt.value = d.plughw;
    opt.textContent = d.name || d.plughw;
    sel.appendChild(opt);
  }
  if (defaultDevice) sel.value = defaultDevice;
  // 下拉框**始终显示**，即使只有一个可选设备。曾经在只剩一项时把整个 select 藏起来
  // （「没有可选项，只占宽度」），但那样用户无从确认当前用的是哪个麦克风——而这正是
  // 录音前最需要看一眼的信息。一个只读的单选项框反而是句明确的声明。
  // 服务端已滤掉 SoC 内部音频中枢（见 services.filter_capture_devices），所以这里
  // 列出的每一项都是真实可用的物理输入。
}

// ---------- 渲染 ----------
function setBarVisible(visible) {
  if (barEl) barEl.hidden = !visible;
}

function renderRecordingUi(recording, seconds) {
  const btn = $('recToggle');
  const dot = barEl && barEl.querySelector('.rec-dot');
  if (!btn) return;
  // 秒数一律夹到非负：syncRecorder 用 Date.now()-started_at 回算真实已过时间，
  // 服务端与浏览器有几秒时钟偏差时会算出极小的负数，formatTime 会显示成 -1:00:00。
  const secs = Math.max(0, seconds || 0);
  btn.textContent = recording ? '停止录音' : '开始录音';
  btn.classList.toggle('recording', !!recording);
  if (barEl) barEl.classList.toggle('active', !!recording);
  if (dot) dot.classList.toggle('on', !!recording);
  const dev = $('recDevice');
  if (dev) dev.disabled = !!recording;
  // 计时行只在录制中出现：空闲时它恒为 00:00:00，没有信息量。
  const meta = $('recMeta');
  if (meta) meta.hidden = !recording;
  const t = $('recTime');
  if (t) t.textContent = formatTime(secs);
  renderGlobalTag(recording, secs);
}

function renderGlobalTag(recording, seconds) {
  if (!recordingTagEl) return;
  recordingTagEl.hidden = !recording;
  if (recording) {
    const s = recordingTagEl.querySelector('.rec-tag-time');
    if (s) s.textContent = formatTime(seconds || 0);
  }
}

function buildGlobalTag() {
  const tb = document.querySelector('#tabTranscript .toolbar .group');
  if (!tb || recordingTagEl) return;
  const el = document.createElement('span');
  el.className = 'rec-tag';
  el.id = 'recTag';
  el.hidden = true;
  el.title = '正在录音（服务端采集）';
  el.innerHTML = '<span class="rec-dot on" aria-hidden="true"></span>录制中 <span class="rec-tag-time">00:00:00</span>';
  tb.appendChild(el);
  recordingTagEl = el;
}

// ---------- 轮询 ----------
function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    const st = await fetchStatus();
    if (!st.recording) {
      // 服务端已不在录（被别的端停掉 / 看护协程收尾）：结束本端轮询并刷新列表
      stopPolling();
      renderRecordingUi(false, 0);
      await refreshFilesFromServer();
      return;
    }
    renderRecordingUi(true, st.recorded_seconds);
  }, POLL_MS);
}

function stopPolling() {
  if (pollTimer != null) { clearInterval(pollTimer); pollTimer = null; }
}

// 录音状态变化后把 payload 的 files 重新拉回（新的录音条目 / done 后的段落都由
// SSE 与这里共同兜底；此处只负责让文件卡片尽快出现在列表里）。
async function refreshFilesFromServer() {
  const id = state.currentMeetingId;
  if (id == null) return;
  try {
    const res = await fetch(`/history/${id}`);
    const data = await res.json();
    if (!res.ok || !data.ok) return;
    if (state.currentMeetingId !== id) return;
    const pfiles = ((data.item && data.item.payload && data.item.payload.files) || []);
    // 只补齐「服务端有、本地无」的条目（录音新条目），不覆盖本地已渲染的段落与编辑
    pfiles.forEach((pf, idx) => {
      if (state.files[idx]) return;
      state.files[idx] = {
        id: pf.id || ('rec' + idx),
        file: { name: pf.name || `录音${idx + 1}`, size: 0 },
        url: pf.has_audio ? `/history/${id}/audio/${idx}` : null,
        status: pf.status || 'pending',
        error: pf.error || '',
        segments: (pf.segments || []).map(s => {
          const seg = {
            start: s.start, end: s.end, speaker: s.speaker, text: s.text || '', flagged: !!s.flagged,
          };
          const ov = (s.speakerName || '').trim();
          if (ov) seg.speakerName = ov;
          return seg;
        }),
        elapsed: pf.elapsed ?? null,
        duration: pf.duration ?? null,
        audio_name: pf.audio_name || '',
        has_audio: !!pf.has_audio,
        // 这里只补「服务端有、本地无」的槽位，正常轮不到墓碑（墓碑一直占着本地下标）。
        // 仍然如实带上：漏了它这条补齐路径就会凭空造出一张空卡片。
        deleted: !!pf.deleted,
      };
    });
    renderFileList();
    updateRunAllState();
  } catch (e) { /* 拉取失败：SSE 快照仍会纠正列表 */ }
}

// ---------- 开始 / 停止 ----------
async function startRecording() {
  const id = state.currentMeetingId;
  if (id == null) { toast('请先新建会议', 'warn'); return; }
  const btn = $('recToggle');
  const device = $('recDevice') ? $('recDevice').value : '';
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/history/${id}/record/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    if (state.currentMeetingId !== id) return;
    await refreshFilesFromServer();
    renderRecordingUi(true, 0);
    startPolling();
    document.dispatchEvent(new CustomEvent('history:changed'));
    toast('已开始录音', 'ok');
  } catch (e) {
    toast('无法开始录音：' + (e.message || '网络错误'), 'err');
    renderRecordingUi(false, 0);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function stopRecording() {
  const id = state.currentMeetingId;
  if (id == null) return;
  const ok = await confirmDialog({
    title: '停止录音',
    message: '停止后将立即对本段录音做整段转写，无法续录。确定停止吗？',
    confirmText: '停止录音',
  });
  if (!ok) return;
  const btn = $('recToggle');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/history/${id}/record/stop`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    stopPolling();
    renderRecordingUi(false, 0);
    await refreshFilesFromServer();
    document.dispatchEvent(new CustomEvent('history:changed'));
    toast('录音已停止，开始转写', 'ok');
  } catch (e) {
    toast('停止失败：' + (e.message || '网络错误'), 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------- 会话同步 ----------
// 切换会议后把录音条对齐到该会议的服务端状态（没有 SSE 也能拿到按钮态与计时起点）。
export async function syncRecorder() {
  if (!available) { setBarVisible(false); stopPolling(); return; }
  setBarVisible(true);
  const st = await fetchStatus();
  renderRecordingUi(!!st.recording, st.recorded_seconds || 0);
  if (st.recording) {
    // 已录时长 = 现在 - 起点：轮询尚未跑第一轮时先按真实已过时间显示，不归零
    if (st.started_at) {
      renderRecordingUi(true, Date.now() / 1000 - st.started_at);
    }
    startPolling();
  } else {
    stopPolling();
  }
}

// SSE 帧里出现「录制中」时对齐本端：别的端（或服务端看护）开始/结束了录音，本端
// 也要跟着轮询或停下来，否则两个标签页会各自计时。由 transcribe-events.js 调用。
export function noteRecordingFrame(status, startedAt) {
  if (!available) return;
  if (status === 'recording') {
    setBarVisible(true);
    const secs = startedAt ? Date.now() / 1000 - startedAt : 0;
    renderRecordingUi(true, secs);
    if (pollTimer == null) startPolling();
  } else if (pollTimer != null) {
    stopPolling();
    renderRecordingUi(false, 0);
  }
}

export function initRecorder() {
  barEl = buildBar();
  buildGlobalTag();
  setBarVisible(false);

  $('recToggle').addEventListener('click', () => {
    const rec = barEl && barEl.classList.contains('active');
    if (rec) stopRecording(); else startRecording();
  });

  // 切会议时由 restore.js / new-meeting.js 派发的 history:active 驱动同步
  document.addEventListener('history:active', () => { syncRecorder(); });

  fetchDevices().then((d) => {
    if (!available) return;   // 开发机无 /proc/asound：整块 UI 不出现
    renderDevices(d.default_device || '');
    syncRecorder();
  });
}
