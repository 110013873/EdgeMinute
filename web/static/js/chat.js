// ============================================================
// 智能问答面板（SSE 流式，Markdown 渲染）
// ============================================================
import { state } from './state.js';
import { $ } from './dom.js';
import { renderMarkdown } from './util.js';
import { speakerLabel } from './speakers.js';
import { toast } from './ui-feedback.js';

const chatFab = $('chatFab');
const chatPanel = $('chatPanel');
const chatBody = $('chatBody');
const chatInput = $('chatInput');
const chatSend = $('chatSend');

export function openChat() {
  chatPanel.classList.add('open');
  chatFab.classList.add('hidden');
  chatInput.focus();
}
function closeChat() {
  chatPanel.classList.remove('open');
  chatFab.classList.remove('hidden');
}

// 收集当前所有转写段落作为上下文（问答 + 总结共用）
export function chatSegments() {
  const out = [];
  for (const item of state.files) if (item.segments.length) for (const seg of item.segments) out.push(seg);
  return out;
}

function renderChat() {
  if (!state.chatHistory.length) {
    chatBody.innerHTML = '';
    chatBody.appendChild($('chatEmpty'));
    return;
  }
  chatBody.innerHTML = '';
  for (const m of state.chatHistory) chatBody.appendChild(buildChatMsg(m));
  chatBody.scrollTop = chatBody.scrollHeight;
}
function buildChatMsg(m) {
  const div = document.createElement('div');
  div.className = 'chat-msg ' + m.role;
  const roleLabel = m.role === 'user' ? '我' : 'AI';
  const role = document.createElement('span');
  role.className = 'role'; role.textContent = roleLabel;
  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (m.error ? ' err' : '');
  // AI 正常回复渲染 Markdown；用户消息与错误提示按纯文本转义显示
  if (m.role === 'assistant' && !m.error) { bubble.classList.add('md'); bubble.innerHTML = renderMarkdown(m.content); }
  else { bubble.textContent = m.content; }
  div.appendChild(role); div.appendChild(bubble);
  return div;
}

function autoGrow() {
  chatInput.style.height = 'auto';
  chatInput.style.height = Math.min(120, chatInput.scrollHeight) + 'px';
}

function setChatBusy(busy) {
  chatInput.disabled = busy;
  chatSend.textContent = busy ? '停止' : '发送';
  chatSend.classList.toggle('stop', busy);
}

async function sendChat() {
  // 进行中 → 点击即中止
  if (state.chatAbort) { state.chatAbort.abort(); return; }
  const text = chatInput.value.trim();
  if (!text) return;
  const segments = chatSegments();
  if (!segments.length) { toast('请先完成音频转写，AI 才能基于会议内容回答', 'warn'); return; }

  state.chatHistory.push({ role: 'user', content: text });
  chatInput.value = '';
  autoGrow();
  renderChat();

  // 占位的 assistant 气泡，流式增量写入
  const asstMsg = { role: 'assistant', content: '' };
  state.chatHistory.push(asstMsg);
  renderChat();
  const bubble = chatBody.querySelector('.chat-msg.assistant:last-child .bubble');
  bubble.classList.add('md');
  bubble.innerHTML = '<span class="cursor">▍</span>';

  state.chatAbort = new AbortController();
  setChatBusy(true);
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        // 不含最后一条空的 assistant 占位
        messages: state.chatHistory.slice(0, -1),
        // speakerMap：把映射后的发言人名（如 0→张三）一并传给后端，
        // 否则模型只看到“说话人0”，无法回答“张三说了什么”
        segments, meta: state.meta, speakerMap: state.speakerMap, stream: true,
      }),
      signal: state.chatAbort.signal,
    });
    if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
    await readSSE(res.body, asstMsg, bubble);
    if (!asstMsg.content) { asstMsg.content = '（无回复）'; bubble.classList.remove('md'); bubble.textContent = asstMsg.content; }
    else { bubble.innerHTML = renderMarkdown(asstMsg.content); }  // 去掉流式游标，定稿
  } catch (e) {
    if (e.name === 'AbortError') {
      asstMsg.content = asstMsg.content || '（已停止）';
    } else {
      asstMsg.error = true;
      asstMsg.content = 'AI 请求失败：' + (e.message || '网络错误');
    }
    // 重建以套用错误样式
    renderChat();
  } finally {
    state.chatAbort = null;
    setChatBusy(false);
    chatInput.focus();
  }
}

// 解析 OpenAI 兼容 SSE 流，增量写入气泡
async function readSSE(stream, asstMsg, bubble) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const events = buf.split('\n\n');
    buf = events.pop();                 // 末尾可能是半个事件，留到下一轮
    for (const evt of events) {
      for (const line of evt.split('\n')) {
        const s = line.trim();
        if (!s.startsWith('data:')) continue;
        const data = s.slice(5).trim();
        if (data === '[DONE]') return;
        try {
          const j = JSON.parse(data);
          if (j.error) { asstMsg.error = true; asstMsg.content = 'AI 错误：' + j.error; renderChat(); return; }
          const delta = j.choices?.[0]?.delta?.content || j.choices?.[0]?.message?.content || '';
          if (delta) {
            asstMsg.content += delta;
            // 流式增量渲染 Markdown（末尾附游标提示仍在生成）
            bubble.innerHTML = renderMarkdown(asstMsg.content) + '<span class="cursor">▍</span>';
            chatBody.scrollTop = chatBody.scrollHeight;
          }
        } catch (e) { /* 忽略无法解析的心跳/分片行 */ }
      }
    }
  }
}

// 从某段转写引用到问答面板：插入「发言人：内容」并把光标置于其后
export function quoteSegmentToChat(item, view) {
  const quote = `「${speakerLabel(view.speaker)}：${(view.text || '').trim()}」\n`;
  const cur = chatInput.value;
  chatInput.value = cur ? (cur.replace(/\s*$/, '') + '\n' + quote) : quote;
  autoGrow();
  chatInput.focus();
  const end = chatInput.value.length;
  chatInput.setSelectionRange(end, end);
  chatInput.scrollTop = chatInput.scrollHeight;
}

export function initChat() {
  chatFab.addEventListener('click', openChat);
  $('chatClose').addEventListener('click', closeChat);
  $('chatClear').addEventListener('click', () => {
    if (state.chatAbort) return;
    state.chatHistory = [];
    renderChat();
  });
  chatInput.addEventListener('input', autoGrow);
  chatSend.addEventListener('click', sendChat);
  chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
}
