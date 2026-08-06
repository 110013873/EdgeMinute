# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**密纪 EdgeMinute** — a fully-local meeting-minutes tool ("Minutes that never leave the room."). Runs on a Jetson (or any CUDA-capable machine). A FastAPI backend loads FunASR models at startup and exposes a `/transcribe` endpoint; a build-step-free frontend under `web/` (`web/index.html` + `web/static/`) handles upload, display, and editing entirely in the browser. Audio and transcripts stay on the local network — nothing goes to the cloud.

## Running the server

```bash
pip install -r requirements.txt
python app.py
# Access at http://<host-ip>:8899
```

Override device if no GPU is available:

```bash
FUNASR_DEVICE=cpu python app.py
```

### Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `FUNASR_DEVICE` | `cuda` | Inference device |
| `LLM_BASE_URL` | `http://127.0.0.1:8080` | OpenAI-compatible LLM base URL for the Q&A panel |
| `LLM_MODEL` | `Qwen3-30B-A3B-Instruct` | Chat model name |
| `LLM_API_KEY` | `` (empty) | Bearer token, if the LLM requires one |
| `LLM_TIMEOUT` | `120` | LLM request timeout (seconds) |
| `CHAT_MAX_CONTEXT_CHARS` | `24000` | Max transcript chars injected into the chat system prompt |
| `HISTORY_DB_PATH` | `data/meetings.db` | SQLite file for meeting history |
| `AUDIO_DIR` | `data/audio` | Root dir for persisted meeting audio (per-meeting subdir) |
| `AUDIO_MAX_BYTES` | `524288000` (500 MB) | Per-audio-file upload cap |
| `HISTORY_PAYLOAD_MAX_BYTES` | `10485760` (10 MB) | Cap on the JSON payload (text) per meeting |
| `SV_ENABLED` | `1` | Voiceprint feature master switch (`0`/`false`/empty → off; also auto-off if the SV model fails to load) |
| `SV_MODEL_ID` | `iic/speech_campplus_sv_zh-cn_16k-common` | Standalone CAM++ speaker-verification model for embedding extraction |
| `VOICEPRINT_DB_PATH` | `data/voiceprints.db` | SQLite file for voiceprint library |
| `VOICEPRINT_EMBEDDING_DIM` | `192` | Expected embedding dimension (validation only) |
| `VOICEPRINT_MATCH_THRESHOLD` | `0.65` | Min cosine similarity to auto-fill a speaker |
| `VOICEPRINT_MAX_SEGMENTS` | `3` | Longest N segments per cluster sampled for the representative embedding |
| `VOICEPRINT_MIN_SEG_SECONDS` | `0.5` | Segments shorter than this are ignored when building the representative embedding |
| `VOICEPRINT_SAMPLE_MAX_BYTES` | `20971520` (20 MB) | Per-file cap on an uploaded enrollment sample |

## Architecture

```
app.py              — FastAPI server; loads models once at startup, serves web/index.html at /
services.py         — model-free pure functions (time/speaker formatting, segment merge,
                      docx template context, LLM system-prompt context, history payload
                      build/validate/column-extract, date grouping)
db.py               — SQLite persistence for meeting history (single `meetings` table:
                      JSON payload column + redundant query columns; WAL + busy_timeout).
                      All sync functions, wrapped in run_in_threadpool by app.py
audio_store.py      — disk storage for persisted meeting audio, one subdir per meeting id;
                      path-traversal-guarded; audio never enters SQLite (payload holds refs)
voiceprints.py      — SQLite persistence for the voiceprint library (two tables:
                      voiceprint_people = one row per enrolled person with an averaged
                      embedding BLOB + sample_count; voiceprint_samples = per-sample
                      embeddings, FK CASCADE). Average is recomputed from samples on every
                      add/delete. All sync functions, wrapped in run_in_threadpool by app.py
sv_embed.py         — thin CAM++ speaker-verification wrapper: holds the SV model, extracts
                      L2-normalized 192-dim embeddings from a file/array, and owns the audio
                      IO (soundfile→16k mono + numpy resample; span slice+concat). Kept
                      separate so IO/model can be stubbed in tests without GPU/soundfile
web/index.html      — frontend shell: 3-column layout, loads static/js/main.js as an ES module
web/logo.svg        — brand logo, served at /logo.svg
web/static/app.css  — all frontend styling
web/static/js/*.js  — frontend logic split into native ES Modules (no bundler). state.js is a
                      single mutable `state` container object (avoids the ESM live-binding
                      reassignment trap); main.js wires + inits every module
templates/minutes.docx — default Word meeting-minutes template (docxtpl / Jinja2 placeholders)
deploy/             — one-shot installer + single systemd unit: install.sh (entrypoint,
                      deploys ASR + local LLM by default; --no-llm to skip),
                      setup_funasr.sh (ASR env), setup_llamacpp.sh (local LLM build + model),
                      start_edgeminute.sh (ExecStart wrapper: backgrounds llama-server +
                      foregrounds web app, LD_PRELOAD libgomp fix), systemd/edgeminute.service.tpl.
                      web + LLM run under ONE `edgeminute` service (cgroup-killed together)
requirements.txt    — pinned deps (fastapi, uvicorn, funasr, httpx, docxtpl, python-docx,
                      numpy, soundfile)
```

**Backend (`app.py`):**
- Models loaded globally at import time: `paraformer-large` (ASR) + `fsmn-vad` + `ct-punc` + `cam++` (speaker diarization)
- `POST /transcribe` saves the upload to a temp file, runs `model.generate()`, converts millisecond timestamps to seconds, returns `{ ok, elapsed_seconds, segments[] }`
- Each segment: `{ start, end, speaker, text }` — speaker is a raw integer from `cam++`
- `POST /export/docx-template` renders `templates/minutes.docx` with `{ meta, segments, options }` via docxtpl and streams back the filled `.docx`. `options` = `{ mergeSpeaker, timestamp, speaker, speakerMap }`. Rendering uses a Jinja env with `autoescape=True` so `< > &` in transcript text don't corrupt the document XML.
- `POST /chat` proxies an OpenAI-compatible `/v1/chat/completions`. The server injects the meeting transcript (merged, budget-truncated) as a `system` message via `build_llm_messages`; front-end-supplied `system` messages are stripped. `stream:true` (default) transparently relays the upstream SSE via `StreamingResponse`; `stream:false` aggregates to `{ ok, content }`.
- `POST /import-agenda` accepts an uploaded `.docx` agenda notice (multipart `file`, ≤5 MB), extracts its text (paragraphs **and** table cells) via `extract_docx_text`, asks the LLM (non-streaming, temperature 0.1) to return strict JSON, and parses it with `parse_agenda_json` (tolerant of code-fences / prose / raw newlines). Returns `{ ok, meta }` where `meta = { title, date, place, agenda, attendees:[{name,unit,title}] }`. Reuses the same LLM env config as `/chat`.

**Voiceprint enrollment + auto-match (声纹登记/自动匹配):**
- Pre-enroll each person's voice once so meetings auto-identify speakers without manual mapping. **File upload only** (no in-browser recording). Uploaded audio is embedded then **discarded** — only the 192-dim vector is stored; no enrollment audio touches disk or SQLite.
- Enrollment/management endpoints (all gated by `SV_ENABLED` + a successfully loaded SV model; return **503** when disabled): `GET /voiceprints` → `{ ok, enabled, threshold, people:[{id,name,unit,title,sample_count}] }`; `POST /voiceprints` (multipart `name`/`unit`/`title` + `file`) enrolls a person from one sample; `POST /voiceprints/{id}/samples` (multipart `file`) appends a sample and re-averages; `GET /voiceprints/{id}/samples`; `DELETE /voiceprints/{id}/samples/{sid}`; `PATCH /voiceprints/{id}` (`{name,unit?,title?}`); `DELETE /voiceprints/{id}`. `_embed_uploaded_sample` writes a temp file, embeds it under `_infer_lock`, and **deletes the temp in `finally`**.
- **Match at transcribe time:** after ASR, `_compute_speaker_matches(audio_path, sentence_info)` (sync, run in threadpool under `_infer_lock`) groups segments by `spk`, builds a representative embedding per cluster (longest `VOICEPRINT_MAX_SEGMENTS` segments ≥ `VOICEPRINT_MIN_SEG_SECONDS`, sliced+concatenated via soundfile/numpy, embedded via CAM++), and cosine-matches against enrolled averages ≥ `VOICEPRINT_MATCH_THRESHOLD`. It **swallows all exceptions → returns `{}`** so matching never breaks transcription. Pure parts live in `services.py` (`build_representative_segments`, `match_speakers`, `cosine_similarity`, `l2_normalize`) — unit-testable without a model.
- **Result plumbing:** result shape is `{ "spk": {name, score} }`, hits only. Foreground `POST /transcribe` returns it as `speaker_matches` in JSON. The background/live path (`_run_asr_from_disk` → `_persist_transcribe_result`) broadcasts a dedicated SSE frame `{type:"speaker_matches", file_index, matches}` — matches are **not persisted on the file payload** (the payload whitelist in `_normalize_history_file` strips unknown fields, and matches would go stale as the voiceprint DB changes; they're a transient hint, re-derivable on re-transcribe).
- **Frontend (silent auto-fill):** `applySpeakerMatches(matches)` fills only `speakerMap` keys the user hasn't set (never overwrites a manual mapping) and records the score in `state.speakerAuto`. `seg.speaker` is **never** rewritten — display names always resolve through `speakerMap` (same invariant as manual mapping). The 映射发言人 panel shows an `自动识别 {score}` badge for auto-filled rows; editing a row clears its `speakerAuto` entry. Library management UI is `web/static/js/voiceprints.js` (🎙 声纹库 toolbar popover).

**Meeting `meta` model:** `{ title, date, place, agenda, attendees:[{name,unit,title}] }`. There is **no `host` or `recorder` field** (both removed). `attendees` is a **structured array**, not a string — `services._normalize_attendees` coerces legacy string / missing forms, and `attendees_to_lines` renders each as `姓名（单位·职务）` (one per line) for both the docx cell and the LLM context.

**Meeting-minutes template (`templates/minutes.docx`):**
- Edit it in Word like any document; placeholders use docxtpl/Jinja2 syntax: `{{ title }}`, `{{ date }}`, `{{ place }}`, `{{ attendees }}`, `{{ agenda }}`, and a segment loop `{%p for seg in segments %}` … `{{ seg.time }}` / `{{ seg.speaker }}` / `{{ seg.text }}` … `{%p endfor %}`, gated by `{% if show_timestamp %}` / `{% if show_speaker %}`.
- `{{ attendees }}` is a single **multi-line string** (`\n`-joined); docxtpl auto-converts `\n` to `<w:br/>`, so each attendee lands on its own line inside the cell.
- To change the exported format, replace this file in place — no code change needed. The context keys above are the contract (`build_template_context` in `services.py`).

**Frontend (`index.html`):**
- State is a single `files[]` array; all UI is rebuilt by `renderFileList()` / `renderResults()` on every state change
- Speaker labels: purely numeric values get a "说话人" prefix; non-numeric values (after rename) are displayed as-is
- `timeupdate` on each `<audio>` drives playback-sync highlighting — the `playingSegEl` global tracks the currently highlighted row
- Speaker dropdown (`replaceFromEl`) is rebuilt from current segment data after every replace/render cycle via `updateSpeakerDropdown()`

## Key constraints

- The server is intentionally single-file and dependency-light; avoid introducing a JS bundler or frontend framework
- Model loading is blocking and happens once — do not move it inside the request handler
- `seg.start` / `seg.end` are in **seconds** (floats); the raw FunASR output is in **milliseconds** and is divided by 1000 in `app.py`
- Business logic that doesn't need the model lives in `services.py` so it stays unit-testable without a GPU/model load — keep new pure logic there, not inline in the route handlers
- The frontend Q&A panel reads `/chat` as an SSE stream; AI output is rendered as Markdown via `renderMarkdown`, which **escapes first (`escapeHtml`) then applies formatting** — it never injects raw model HTML, preserving the anti-injection guarantee
- Voiceprint matching is **advisory only**: it fills `speakerMap` (display names), never `seg.speaker`, and never overwrites a manual mapping. `speaker_matches` is transient (JSON return / SSE frame) and deliberately **not** stored in the history payload — do not add it to the payload whitelist. All SV audio IO stays in `sv_embed.py` (soundfile + numpy, no ffmpeg) so tests can stub it without decoding real audio.
- `meta.attendees` is an array of `{name,unit,title}`; the panel renders a structured editor (add/remove rows). `normalizeMeta` is the single coercion boundary — every external `meta` (old saved sessions, `/import-agenda` results, undo/redo restore) passes through it, so legacy string-`attendees` / stray `host` are handled in one place. Keep new `meta` consumers going through `normalizeMeta` / `attendeeLines`.
