<div align="center">

# 密纪 · EdgeMinute

**会议不上云，纪要不外传。**
_Minutes that never leave the room._

一个完全本地化的会议转写与纪要工具：语音转写 · 说话人分离 · 声纹识别 · AI 纪要 —— 音频与转写数据全程留在内网，不出域。

[English](#english) · [快速开始](#快速开始) · [一键安装](#一键安装--自启动) · [配置](#配置环境变量)

</div>

---

## ✨ 特性

- 🔒 **安全保密** — 纯本地内网部署，所有音频、转写数据均留存内网环境，数据不出域，充分保障涉密内部会议的数据安全。全流程无任何云端调用。
- 🎙 **语音转写 + 说话人分离** — 基于 [FunASR](https://github.com/modelscope/FunASR)（Paraformer + VAD + 标点 + CAM++），自动切分句子、标注时间轴，并区分不同发言人。
- 🧬 **声纹比对** — 内置说话人分离能力对接声纹库，预先登记每人一段声纹后，开会自动识别发言人并直接标注参会人真实姓名（仅存 192 维向量，登记音频用完即弃，不落盘、不入库）。
- 🔤 **热词定制** — 支持导入自定义业务高频热词库，优化行业术语、专有名词、人名地名的识别效果（如「灵玑」不再误识为「灵机」）。
- 📄 **议程解析** — 上传会议议程 Word 文档，由 LLM 智能解析，自动提取标题、时间、地点、议程、参会人等结构化关键信息并回填。
- 📝 **纪要生成** — 一键生成结构化会议纪要，萃取议题、发言要点、决议、待办等关键信息；纪要 Word 模板可自由定制（`templates/minutes.docx`）。
- ⏱ **超长会议** — 支持超长会议录音处理，多段录音文件统一归档管理，历史会议自动按日期分组，方便回溯查阅。
- ✏️ **内容校对** — 录音转写文本支持可视化编辑、人工校对，可对识别错误的内容逐句修正；发言人映射、合并连续段、查找/替换、撤销/重做一应俱全。
- 💬 **智能问答** — 基于本次会议全部内容实现本地 AI 智能问答，可针对会议内容进行查询、摘要、信息检索，回答以 Markdown 流式渲染。
- 🧩 **零构建前端** — 单页 + 原生 ES Modules，无打包器、无前端框架，改完刷新即用。
- ⚡ **一键安装 + 自启动** — 一条命令装好环境并注册 systemd 服务，开机自启。

## 🏗 架构

```
浏览器 (web/)  ──HTTP/SSE──▶  FastAPI (app.py)  ──▶  FunASR 模型（转写/分离/声纹）
                                     │
                                     ├──▶ SQLite（会议历史 / 声纹库）
                                     ├──▶ 本地磁盘（会议音频）
                                     └──▶ OpenAI 兼容 LLM（纪要 / 问答 / 议程解析）
```

模型在服务启动时**一次性加载**；后端纯函数逻辑集中在 `services.py`，无需 GPU 即可单元测试。

## 📦 目录结构

```
EdgeMinute/
├── app.py                    # FastAPI 入口（启动时加载模型，服务 web/ 前端）
├── services.py               # 无模型依赖的纯函数（格式化/合并/模板上下文/校验）
├── db.py  audio_store.py     # 会议历史 SQLite / 会议音频磁盘存储
├── voiceprints.py sv_embed.py# 声纹库 SQLite / CAM++ 声纹向量提取
├── transcribe_jobs.py        # 后台/实时转写任务
├── web/                      # 前端（index.html + logo.svg + static/）
├── templates/minutes.docx    # 纪要 Word 模板（docxtpl / Jinja2）
├── deploy/                   # 一键安装脚本 + systemd 单元模板
├── requirements.txt
├── .env.example              # 环境变量样例
└── README.md
```

## 🚀 快速开始

**硬件**：Jetson（JetPack 5.1.2 / Python 3.8 / CUDA 11.4）实测可用；或任意 CUDA 机器；无 GPU 可用 `FUNASR_DEVICE=cpu`（较慢）。

```bash
# 1. 安装依赖（首次运行会自动下载 FunASR 模型）
pip install -r requirements.txt

# 2. 启动
python app.py
#   无 GPU：  FUNASR_DEVICE=cpu python app.py

# 3. 浏览器访问
#   http://<本机IP>:8899
```

> 纪要与问答面板需要一个 OpenAI 兼容的 LLM 服务。`deploy/install.sh` **默认会在本机一并部署并启动
> 本地 LLM**（llama.cpp + Qwen3）；若手动跑 `python app.py`，请通过 `LLM_BASE_URL` 指向一个可用的
> LLM 地址（见[配置](#配置环境变量)），默认指向本机 `http://127.0.0.1:8080`。

## ⚙️ 一键安装 + 自启动

`deploy/install.sh` 会安装 ASR 环境、**默认一并部署本地 LLM**（llama.cpp + Qwen3），
并把两者注册为**同一个** `edgeminute` systemd 服务实现开机自启：

```bash
# 默认：ASR + 本地 LLM + Web，全部注册开机自启（单一 edgeminute 服务托管）
sudo ./deploy/install.sh

# 不部署本地 LLM，改用外部 LLM_BASE_URL（局域网其它机器 / 已在跑的 llama.cpp）
sudo ./deploy/install.sh --no-llm

# 已装好环境，只想重新注册服务（会复用已编译的 llama.cpp 与已下载的模型）
sudo ./deploy/install.sh --skip-setup

# 自定义端口 / 虚拟环境 / 运行用户
sudo ./deploy/install.sh --port 8899 --venv /opt/funasr-env --user edgeminute

# LLM 调优：上下文长度 / 量化档位（显存吃紧时调小 ctx）
sudo ./deploy/install.sh --llm-ctx 32768 --llm-quant Q4_K_M
```

> **默认自带本地 LLM。** 安装脚本会编译 llama.cpp、下载 Qwen3 GGUF 模型（约数十 GB）并随 web
> 一同启动。若你已有可用的 LLM 服务，加 `--no-llm` 跳过本机部署，再在 `.env` 里把 `LLM_BASE_URL`
> 指过去即可。web 与 LLM 由**同一个** `edgeminute` 服务托管（`start_edgeminute.sh` 后台拉起
> llama-server、前台运行 web），停止/重启时一起收放，无需单独维护两个服务。

装完后：

```bash
journalctl -u edgeminute -f        # 查看日志（web + LLM 同一服务）
sudo systemctl restart edgeminute  # 重启
sudo systemctl status  edgeminute  # 状态
```

`deploy/` 内各脚本也可单独运行：`setup_funasr.sh`（ASR 环境）、`setup_llamacpp.sh`（本地 LLM 编译 + 模型下载）、`start_edgeminute.sh`（systemd ExecStart 包装：后台 llama-server + 前台 web，处理 aarch64 libgomp 预加载）。

## 🔧 配置（环境变量）

复制样例后按需修改：`cp .env.example .env`。常用项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FUNASR_DEVICE` | `cuda` | 推理设备（`cpu` / `cuda`） |
| `EDGEMINUTE_PORT` | `8899` | Web 服务端口 |
| `LLM_BASE_URL` | `http://127.0.0.1:8080` | OpenAI 兼容 LLM 地址 |
| `LLM_MODEL` | `Qwen3-30B-A3B-Instruct` | 对话模型名 |
| `LLM_API_KEY` | 空 | LLM Bearer Token（如需要） |
| `HISTORY_DB_PATH` | `data/meetings.db` | 会议历史 SQLite |
| `AUDIO_DIR` | `data/audio` | 会议音频存储根目录 |
| `SV_ENABLED` | `1` | 声纹功能总开关 |
| `VOICEPRINT_MATCH_THRESHOLD` | `0.65` | 声纹自动匹配的最小余弦相似度 |

完整清单见 [`.env.example`](.env.example)。

## 🔌 主要接口

| 方法 & 路径 | 用途 |
|---|---|
| `POST /transcribe` | 上传音频，返回分段转写 + 声纹匹配 |
| `POST /summarize` · `POST /chat` | 生成纪要 / 会议问答（SSE 流式） |
| `POST /import-agenda` | 上传议程 Word，LLM 提取结构化元信息 |
| `POST /export/docx-template` | 按模板导出 `.docx` 纪要 |
| `GET/POST/PATCH/DELETE /voiceprints…` | 声纹库登记与管理 |
| `GET/POST/DELETE /history…` | 会议历史归档与回溯 |

## 🤝 贡献

欢迎 Issue / PR。改动前端后无需构建，直接刷新即可；后端纯逻辑请放在 `services.py` 以保持可测试性。

## 📄 License

[MIT](LICENSE) © 2026 EdgeMinute (密纪)

---

<a name="english"></a>

## English

**EdgeMinute (密纪)** is a fully-local meeting-minutes tool — _Minutes that never leave the room._

Speech-to-text, speaker diarization, voiceprint identification, and AI-generated minutes, all running on your own hardware/LAN. No audio or transcript ever goes to the cloud.

- 🔒 **Confidential by design** — fully local, on-prem/LAN deployment; audio and transcripts never leave the internal network. No cloud calls anywhere in the pipeline.
- 🎙 **Transcription + diarization** via [FunASR](https://github.com/modelscope/FunASR) (Paraformer + VAD + punctuation + CAM++): sentence segmentation, timestamps, and per-speaker labels.
- 🧬 **Voiceprint matching** — enroll each speaker once, then meetings auto-identify who's speaking and fill in real names. Only a 192-dim vector is stored; enrollment audio is discarded (never written to disk or DB).
- 🔤 **Custom hotwords** — import a domain hotword list to improve recognition of jargon, proper nouns, names, and places.
- 📄 **Agenda parsing** — upload an agenda `.docx`; the LLM extracts structured metadata (title, date, place, agenda, attendees) and fills the form.
- 📝 **Minutes generation** — one click produces structured minutes (topics, key points, decisions, action items) via a fully customizable `.docx` template.
- ⏱ **Long meetings** — handles very long recordings and multi-file archives, with history auto-grouped by date for easy retrieval.
- ✏️ **Proofreading** — visual, line-by-line editing of the transcript, plus speaker mapping, segment merging, find/replace, and undo/redo.
- 💬 **AI Q&A** — ask questions, summarize, or search over the full meeting content with a local LLM; answers stream in as Markdown.
- ⚡ **One-shot install + auto-start** via `deploy/install.sh` (systemd).

### Quick start

```bash
pip install -r requirements.txt
python app.py            # or: FUNASR_DEVICE=cpu python app.py
# open http://<host-ip>:8899
```

### One-shot install + auto-start

```bash
sudo ./deploy/install.sh            # ASR + local LLM + web, all as one edgeminute service (default)
sudo ./deploy/install.sh --no-llm   # skip the local LLM; point LLM_BASE_URL at an external one
```

The web app and the local LLM run under a **single** `edgeminute` systemd unit
(`start_edgeminute.sh` launches llama-server in the background and the web app in
the foreground); `systemctl restart edgeminute` restarts both together.

Runs on a Jetson (JetPack 5.1.2 / Python 3.8 / CUDA 11.4) or any CUDA machine; CPU-only works via `FUNASR_DEVICE=cpu`. See [`.env.example`](.env.example) for all configuration. Licensed under [MIT](LICENSE).
