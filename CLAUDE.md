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
| `LLM_TIMEOUT` | `1800` | LLM request timeout (seconds) for chat/agenda |
| `SUMMARY_TIMEOUT` | `1800` | Timeout for the meeting-summary call (non-streaming, long output); floored to `LLM_TIMEOUT` |
| `CHAT_MAX_CONTEXT_CHARS` | `24000` | Max transcript chars injected into the chat system prompt |
| `HISTORY_DB_PATH` | `data/meetings.db` | SQLite file for meeting history |
| `AUDIO_DIR` | `data/audio` | Root dir for persisted meeting audio (per-meeting subdir) |
| `AUDIO_MAX_BYTES` | `524288000` (500 MB) | Per-audio-file upload cap |
| `HISTORY_PAYLOAD_MAX_BYTES` | `10485760` (10 MB) | Cap on the JSON payload (text) per meeting |
| `SV_ENABLED` | `1` | Voiceprint feature master switch (`0`/`false`/empty → off; also auto-off if the SV model fails to load). Gates **voice** operations only — voiceless contacts stay manageable when off |
| `SV_MODEL_ID` | `iic/speech_campplus_sv_zh-cn_16k-common` | Standalone CAM++ speaker-verification model for embedding extraction |
| `CONTACTS_DB_PATH` | `data/contacts.db` | SQLite file for the 通讯录 (contacts, incl. voiceprints). **Falls back to `VOICEPRINT_DB_PATH` if unset** — existing deployments keep pointing at their old `data/voiceprints.db` with no config change; the DB is migrated in place on first boot |
| `VOICEPRINT_DB_PATH` | *(unset)* | Legacy alias for `CONTACTS_DB_PATH`, kept for现网 compatibility. If both unset, defaults to `data/contacts.db` |
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
contacts.py         — SQLite persistence for the 通讯录 (contacts directory, incl. optional
                      voiceprints). Two tables: `contacts` = one row per person (name/unit/
                      title + averaged embedding BLOB + sample_count; **sample_count=0 ⇒
                      voiceless contact**, embedding is a zero placeholder); `contact_samples`
                      = per-sample embeddings, FK CASCADE. Average recomputed on every add/
                      delete. `add_contact()` adds a voiceless person; `enroll_person()` adds
                      one with a first sample; `get_all_embeddings()` filters `sample_count>0`
                      so voiceless contacts never join matching. **Idempotent legacy migration**
                      in `init_db()`: renames old `voiceprint_people`/`voiceprint_samples`
                      tables (+ `person_id`→`contact_id` column) in place, zero data loss.
                      Upgraded from the former voiceprints.py — the SV/matching machinery
                      (sv_embed, services.match_speakers, SV_*/VOICEPRINT_* params) keeps its
                      voiceprint naming; only the people-directory surface became contacts.
                      All sync functions, wrapped in run_in_threadpool by app.py
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

**通讯录 (contacts) — directory + optional voiceprint (含声纹的常用联系人):**
- The 通讯录 is a directory of frequently-used people (name/unit/title). A **voiceprint is an optional attribute** of a contact: enroll one and meetings auto-identify that speaker; skip it and the contact is still a reusable directory entry. Contacts feed **参会人员 one-click import** (see below); voice-enrolled ones additionally power transcribe-time speaker matching.
- **Voiceless vs voice-enrolled:** a contact with `sample_count=0` is voiceless (zero-embedding placeholder, excluded from matching by `get_all_embeddings()`'s `sample_count>0` filter). `GET /contacts` adds a computed `has_voice = sample_count>0` per row for the UI.
- Voiceprint audio is **file upload only** (no in-browser recording), embedded then **discarded** — only the 192-dim vector is stored; no enrollment audio touches disk or SQLite. `_embed_uploaded_sample` writes a temp file, embeds under `_infer_lock`, **deletes the temp in `finally`**.
- **Contacts endpoints:** `GET /contacts` → `{ ok, enabled, threshold, people:[{id,name,unit,title,sample_count,has_voice}] }` (name-sorted; **not gated** by SV — always lists). `POST /contacts` (JSON `{name,unit?,title?}`) adds a **voiceless** contact (not SV-gated). The following are gated by `SV_ENABLED` + a loaded SV model (**503** when disabled): `POST /contacts/voice` (multipart `name`/`unit`/`title` + `file`) enrolls a contact with a first sample; `POST /contacts/{id}/samples` (multipart `file`) appends a sample / adds voice to a voiceless contact and re-averages; `GET /contacts/{id}/samples`; `DELETE /contacts/{id}/samples/{sid}`. `PATCH /contacts/{id}` (`{name,unit?,title?}`) and `DELETE /contacts/{id}` are not SV-gated. The transcript-row annotate route keeps its path `POST /history/{meeting_id}/audio/{file_index}/voiceprint` (voiceprint naming intentional).
- **Match at transcribe time:** after ASR, `_compute_speaker_matches(audio_path, sentence_info)` (sync, run in threadpool under `_infer_lock`) groups segments by `spk`, builds a representative embedding per cluster (longest `VOICEPRINT_MAX_SEGMENTS` segments ≥ `VOICEPRINT_MIN_SEG_SECONDS`, sliced+concatenated via soundfile/numpy, embedded via CAM++), and cosine-matches against enrolled averages ≥ `VOICEPRINT_MATCH_THRESHOLD`. It **swallows all exceptions → returns `{}`** so matching never breaks transcription. Pure parts live in `services.py` (`build_representative_segments`, `match_speakers`, `cosine_similarity`, `l2_normalize`) — unit-testable without a model.
- **Result plumbing:** result shape is `{ "spk": {name, score} }`, hits only. Foreground `POST /transcribe` returns it as `speaker_matches` in JSON. The background/live path (`_run_asr_from_disk` → `_persist_transcribe_result`) broadcasts a dedicated SSE frame `{type:"speaker_matches", file_index, matches}` — matches are **not persisted on the file payload** (the payload whitelist in `_normalize_history_file` strips unknown fields, and matches would go stale as the contacts DB changes; they're a transient hint, re-derivable on re-transcribe).
- **Frontend (silent auto-fill):** `applySpeakerMatches(matches)` fills only `speakerMap` keys the user hasn't set (never overwrites a manual mapping) and records the score in `state.speakerAuto`. `seg.speaker` is **never** rewritten — display names always resolve through `speakerMap` (same invariant as manual mapping). The 映射发言人 panel shows an `自动识别 {score}` badge for auto-filled rows; editing a row clears its `speakerAuto` entry.
- **Frontend modules:** `web/static/js/contacts.js` (`initContacts`, `contactsBtn`/`contactsPop` — 通讯录 toolbar popover; add voiceless contact or enroll-with-voice, 录声纹/＋样本 per row, `未录声纹` badge, non-blocking 同名+同单位 dedup warning; exports `fetchContacts()`). `contact-picker.js` (`pickContacts()` → reusable multi-select `<dialog>` returning chosen `{name,unit,title}[]`). `attendee-merge.js` (`mergeAttendees(existing, incoming)` pure fn → `{merged, added, skipped}`, dedup by 同名+同单位, unit-tested).

**参会人员 one-click import (从通讯录导入):** both the 会议信息 panel (`importContactsBtn`, `meta-panel.js`) and the 新建会议 dialog (`nmImportContactsBtn`, `new-meeting.js`) open `pickContacts()`, then `mergeAttendees()` pushes chosen contacts into `attendees` **immutably** (meta panel via the `state.meta = {...}` / `normalizeMeta` boundary; dialog via its local `attendees` array), skipping 同名+同单位 duplicates with a skip-count toast. Import feeds attendees only — it does **not** alter the 映射发言人 dropdown, which stays sourced from `attendeeList()`.

**表格内改发言人 (transcript speaker edit):** `segments.beginSpeakerEdit` — when `attendeeList()` is non-empty, the inline editor attaches a `<datalist>` of attendee names so the user can **pick or free-type** (select-or-type); empty attendees → plain input. Commit still routes through `renameSpeakerLabel` (speakerMap only, never `seg.speaker`); Enter/Escape/blur behavior unchanged.

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
