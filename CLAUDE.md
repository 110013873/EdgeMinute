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
| `RECORD_ENABLED` | `1` | USB-mic recording master switch (`0`/`false`/empty → off; `/record/devices` reports `enabled:false`, the record routes 503, and the frontend recording bar stays hidden) |
| `RECORD_DEVICE` | *(empty)* | Default capture device as an ffmpeg ALSA string, e.g. `plughw:CARD=Device,DEV=0`. Empty → auto-pick the first enumerated capture device |
| `RECORD_MAX_BYTES` | `10737418240` (10 GB) | Per-recording size cap; the watchdog auto-stops when reached. **Separate from `AUDIO_MAX_BYTES`** — that one is only checked inside `save_audio`, and ffmpeg writes the recording straight to disk without going through it. Sized for the 192 KB/s capture rate (48 kHz stereo) ≈ 15.5 h |

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
                      IO (16k mono: soundfile fast path only when the file is already
                      16k mono, otherwise ffmpeg — the old numpy linear resample aliased
                      when downsampling, which the 48k capture made a routine path;
                      span slice+concat). Kept
                      separate so IO/model can be stubbed in tests without GPU/soundfile
transcribe_jobs.py  — TranscribeManager: in-process job registry + SSE event bus. `submit()`
                      queues a job per (meeting, file index), `broadcast(meeting_id, event)`
                      is the single public injection point every SSE subscriber hangs off,
                      `snapshot_event()` renders the reconnect frame (incl. `recording` state)
recorder.py         — RecorderManager: server-side USB-mic capture (ffmpeg + ALSA). Same shape
                      as transcribe_jobs — in-process registry, no DB, no queue; owns the
                      subprocess lifecycle + crash recovery. `emit`/`on_finalize` are injected
                      by app.py, so it never imports app/db and is testable without ffmpeg
web/index.html      — frontend shell: 3-column layout, loads static/js/main.js as an ES module
web/logo.svg        — brand logo, served at /logo.svg
web/favicon.svg     — 标签页站点图标（麦克风），served at /favicon.svg; linked from
                      index.html via <link rel="icon">. A dark rounded tile behind the
                      accent-coloured glyph, since a bare transparent glyph disappears
                      against a dark tab bar.
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

**麦克风录音 (USB mic — server-side capture):**
- Capture happens **on the server**, not in the browser. The mic is physically plugged into the Jetson, and the browser usually reaches the app over LAN `http://<host>:8899` — a non-secure context where `getUserMedia` is refused outright. `recorder.py` therefore shells out to `ffmpeg -f alsa`.
- **Zero new Python dependencies.** `ffmpeg` is already installed by `deploy/setup_funasr.sh` and already invoked by `app.py` / `sv_embed.py`; `requirements.txt` is unchanged. Devices are addressed by CARD **name** (not index — USB device numbers move across reboots) via `services.list_capture_devices(RECORD_DEVICE)` parsing `/proc/asound/cards` + `pcm`, using `plughw:` so ALSA aligns the device's native format to the `REC_*` capture format.
- **Capture is 48 kHz stereo, chosen for playback quality — not for ASR.** `services.REC_SAMPLE_RATE/CHANNELS/BITS = 48000/2/16`. The recording is later *also* a playback file (the same file satisfies both consumers — see the "stopped recording is an ordinary file" invariant), and 16 kHz has an 8 kHz Nyquist ceiling that decapitates the fricatives (s/sh/f/x/c/q, the t/k/p bursts) carrying most of speech intelligibility: the room sounds clear live, the playback sounds muffled. Transcoding to 16 kHz mono belongs to the ASR side, which already does it unconditionally for **every** input including uploads (`app.py::_ensure_audio_input` → ffmpeg → 16 k mono + the denoise/loudness chain), so recording and upload share one code path. That is also the better resampler: ffmpeg's swr is polyphase with a real anti-alias filter, whereas ALSA `plughw`'s default converter had none. **Do not "optimize" the capture rate back to 16 kHz** — that reverts the fix, and it buys nothing, since the ASR-side conversion happens either way.
- **Internal audio hubs are filtered out of the device list** (`services.filter_capture_devices`). On AGX Orin the Tegra APE card reports a whole bank of `capture`-capable PCM ports (ADMAIF / I2S / DMIC / MVC) — each one *can* be opened as a capture stream, but none is a physical input, and they all render under the same card name, burying the one real mic. `_HUB_MARKERS` + a word-boundary `\bape\b` drop them. Two fallbacks keep this from ever removing the recording entry itself: the card named by `RECORD_DEVICE` is never filtered (explicit config wins), and an all-filtered list is returned as-is. **Filtering happens inside `list_capture_devices`, not at the call sites** — that also fixes `pick_capture_device`'s `devs[0]` fallback, which otherwise selected the lowest-numbered internal hub and recorded silence. `RECORD_DEVICE=plughw:CARD=Speakerphone,DEV=0` on the deployed unit means `.env` must be loaded for manual runs (`set -a; . ./.env; set +a`) or the auto-pick takes over silently.
- **Raw PCM, header synthesized at stop.** ffmpeg writes `<idx>_mic.raw` (`-f s16le`), never a container: a WAV header needs ffmpeg to seek back and backfill the size field on a clean exit, so a SIGKILL/power-loss leaves a container file worthless — or, if nothing was written yet, 0-length. A headerless stream has **no invalid prefix**, so any truncation is still valid audio. On stop: SIGINT → `wait()` → fallback `kill()` → `services.wrap_raw_to_wav(raw_path, wav_path)` writes the 44-byte canonical PCM header from the **actual file size** → `<idx>_mic.wav`. The same repair runs at startup for any leftover `.raw` (`_recover_interrupted_recordings`, called from the lifespan *after* `_reset_stale_transcribing`, which explicitly excludes `recording`). Consequences: recorded length is exactly `size / 192000`, the level meter just reads the tail bytes (`pcm_rms_tail`), and **playback during recording is deliberately unavailable** — that is the price paid for crash safety.
- **`-flush_packets 1` is load-bearing, not a tuning knob.** s16le goes through AVIO's buffer by default, so the raw file only grew in ~8.19 s chunks (262144 bytes) — which made the timer and level meter visibly jump every 8 seconds, and silently capped crash loss at *8 seconds* of audio rather than the "seconds" the raw-stream design was supposed to guarantee (measured: SIGKILL after 10 s of capture left 8 s on disk without the flag, 10 s with it). `recorder.build_capture_cmd` passes it; the cost is nil even at 192 KB/s (it is one write syscall per packet, independent of bitrate).
- **`recorded_seconds` is wall-clock, not file-derived.** `RecorderManager.recorded_seconds` returns `max(now - started_at, raw_pcm_seconds(size))` — the wall clock is what "how long have we been recording" actually means, and the file size is only a lagging lower bound on it (the file may even not exist yet for the first moments of a capture). Both terms use the same injected `clock`, so no server/browser skew is involved; the frontend just displays the number. Reading only the file size is what produced the 8-second stutter in the UI.
- **No inference during recording.** Stop → `pending` → the existing `_jobs.submit` path transcribes the whole file once, exactly as if it had been uploaded. So speaker-diarization quality is *equivalent to uploading the same audio*, `cam++` clusters within a single `model.generate()` call (integer speaker ids stay self-consistent for the whole meeting — no cross-window reconciliation problem exists), and no GPU work contends with the capture.
- **File-level status `"recording"`** (sibling of `pending`/`queued`/`processing`/`done`/`error`), *not* a reuse of `processing` — `processing` drives the frontend's duration-estimated fake progress bar, which is meaningless for a length that is still growing. Plumbing: `services._FILE_ACTIVE` includes it so `derive_meeting_status` reports the meeting as `transcribing`; `snapshot_event(payload, active_indices, started_at, recordings)` takes `recordings: {file_index: started_at}` so a refreshed page gets the timer's origin rather than restarting at 0; the SSE route passes `_recorder.active_started_at(meeting_id)`.
- **Record endpoints:** `GET /record/devices` → `{ok, enabled, devices, default_device}` (`enabled` = `RECORD_ENABLED` **and** at least one device probed). `POST /history/{meeting_id}/record/start` appends the file entry (`status:"recording"`, `audio_name` already pointing at the *post-wrap* `.wav`) and rolls it back if ffmpeg fails to spawn; 409 when that meeting is already recording. `POST /history/{meeting_id}/record/stop`. `GET /history/{meeting_id}/record` → `{ok, recording, file_index, started_at, recorded_seconds, level}` — the no-SSE fallback that lets a reopened browser restore the button state and the timer. `DELETE /history/{meeting_id}` calls `_recorder.stop_meeting()` **before** deleting, so the recording is finalized rather than lost.
- `audio_store.recording_target(meeting_id, raw_name)` is path-traversal-guarded like `audio_path`, and exists because ffmpeg needs a target path to write to — `save_audio` opens `"wb"` and only ever writes once, so recordings must not go through it.
- **Autosave guard (deliberately narrow):** while a meeting is recording, `_apply_history_update` re-stamps the active recording's file slot from the stored copy via `services.preserve_recording_file` — otherwise the frontend's `autosave.js` PUT, built from a memory state that predates the recording, would erase the entry. It preserves **only that one slot**; everything else autosaves normally.

**删除单段录音 (per-card delete) — the non-renumbering tombstone:**
- `DELETE /history/{meeting_id}/files/{file_index}` clears one slot **in place** rather than splicing the array. Deletion is **not** array surgery because `file_index` is simultaneously the array position, the route param, the SSE frame key, the audio-filename prefix (`{index}_mic.wav`), and the in-flight job's identity — shifting any index desyncs all five at once, and would additionally break `_recover_interrupted_recordings`'s index-keyed startup scan. The tombstones also make the desync *silent*: `audio_name` (not the index) is the read-path source of truth, so a shifted file still plays correctly from the wrong slot while every index-keyed route quietly addresses the wrong recording.
- A tombstone is `services.tombstone_file(entry)` — `{id, name}` kept (so the card's identity survives), `duration`/`elapsed`/`audio_name`/`segments` cleared, `has_audio: False`, `status: "deleted"`, `deleted: True`. `is_deleted_file` tolerates old data with no `status` key. `_normalize_history_file` is the authority: `deleted: true` force-clears segments/audio **at the boundary**, so no writer can smuggle content back into a tombstone.
- **`derive_meeting_status` needed no change** — a tombstone's `status` is outside `_FILE_ACTIVE` and its segments are empty, so it degrades to `draft`/`done` on its own.
- **Three guards, one shape.** Every writer that could resurrect a deleted card is blocked, because the DELETE route and a stale frontend PUT/job callback can be in flight simultaneously:
  - `_apply_history_update` runs `services.preserve_deleted_files` — monotone: if the **stored** slot is a tombstone, it is stamped back over the incoming PUT unconditionally. Safe because deletion has no undo entry, so no legitimate PUT can ever turn a tombstone back into content. (This widened that function's DB read from conditional to unconditional — one extra read per PUT, accepted.)
  - `_persist_transcribe_result` / `_persist_record_result` drop their result (and, for the recording path, delete the audio) when the target slot is a tombstone — the "job submitted → user deletes → job finishes" window.
  - `history_transcribe` rejects a tombstone slot with 404 rather than falling through to the misleading "音频尚未上传" message.
- **The route 409s rather than allowing a delete that would corrupt state:** the slot must not be the one currently recording (`_recorder.active_file_index` — ffmpeg is writing its `.raw`) nor have an in-flight job (`_jobs.active_file_indices` — the completion callback writes back *by index*). Deleting an already-deleted slot is idempotent **200**, not an error. Audio is removed only **after** the DB write succeeds, so a failed write never leaves "card gone, audio gone"; the reverse failure leaves an orphan file inside a live meeting dir, cleaned up with the meeting.
- **Frontend mirrors the server exactly — it never renumbers either.** `renderFileList` skips tombstones (they occupy no space) but `state.files` keeps every slot, so `state.files.indexOf(item)` stays equal to `file_index`. `upload.js` `deleteFile(index)` → `applyTombstone(index)`; `applyTombstone` is also the handler for **another tab's** delete, driven by the `{type:"file", status:"deleted"}` SSE frame in `transcribe-events.js`. `fileToPayload` must carry `deleted` or the very next autosave resurrects the card. `updateRunAllState`, `renderResults`, `export.js`, and `undo.restore` all filter tombstones — the undo filter matters most, since a snapshot restore is otherwise a back door around an irreversible delete.
- **Cross-module refresh goes through the `files:changed` DOM event**, not an import: `applyTombstone` dispatches it and `main.js` re-runs `ensureSpeakerColors` / `refreshSpeakerFilter` / `renderResults` / `updateRunAllState` / `updatePlaybar`. An `upload.js → segments.js` import would close a cycle (`segments.js` already imports `updateRunAllState` from `upload.js`).
- **Playback follows the same rule.** Deleting the file the player is on moves it to the next live file; deleting the *last* one clears `currentItem` **and** the audio src explicitly — `loadIntoPlayer(null)` returns early and would otherwise leave the playbar showing a revoked blob.

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

**发言人显示名 — 三层解析（批量走面板、单行走行内）:**
- `seg.speaker` is the raw cam++ cluster id and is **never rewritten** — it keys merging, colouring, `refreshSpeakerFilter`, the 映射面板's speaker list, and `voiceprint-annotate.js`'s cluster key. A per-row rename is therefore a **new field**, not a rewrite of `speaker`/`speakerMap`.
- Resolution order, mirrored on both sides (**change one, change the other** — `services.resolve_segment_speaker` and `speakers.segmentLabel`):
  1. `seg.speakerName` — **per-row override**, written by the inline editor in the transcript list.
  2. `speakerMap[seg.speaker]` — the **「映射发言人」panel**, cluster-wide (today's behaviour, unchanged).
  3. Default — `说话人N` / the value as-is.
- **Division of labour, and why both exist:** cam++ sometimes collapses two people into one cluster (张三 and 李四 both land on `speaker 0`). The 映射面板 cannot fix that — it is cluster-wide by construction. The row editor fixes exactly that, per row. Conversely a genuine whole-cluster rename still belongs in the panel. Neither replaces the other.
- `setRowSpeaker(item, idxs, name)` (`speakers.js`) writes/clears `speakerName`; it skips rows already showing that name, so a row that was never really modified does not get pinned out of later cluster mappings. `clearRowSpeakers()` is wired to the panel's **全部还原**, which clears `speakerMap` **and** row overrides together — clearing half of it looks like the button did nothing.
- **`merge_segments` now merges by resolved display name, not raw `speaker`** (`services.py`), and `export.js`'s TXT `collectSegments` mirrors it. Otherwise a row you just split back into 李四 gets swallowed by its neighbouring 张三 segments on export/summary/问答 — the fix would be invisible everywhere except the list.
- **Colour follows the display name** (`nameColors` / `speakerNameColor`): a cluster's swatch stays keyed to `speakerColors`, but each rendered row is coloured by its resolved name. Keeping the cluster colour would make the two split rows the same colour, i.e. visually indistinguishable. `ensureSpeakerColors` seeds name colours for cluster display names first (so they match the panel) and then for any override-only name; `restore.js` clears `nameColors` alongside `speakerColors` on meeting switch.
- **Payload whitelist:** `_normalize_history_file` carries `speakerName` (trimmed, and **omitted entirely when blank** so old payloads round-trip byte-identically). It must be plumbed through every segment-mapping site or the override silently dies on the next autosave / reopen: `history-io.js` (`fileToPayload`), `restore.js`, `transcribe-events.js` (`pullSegments`), `recorder.js`. Undo/redo needs nothing — `undo.js` serialises whole `f.segments`.
- The 映射面板 annotates a cluster with 「N 段已单独修改」 when some of its rows carry an override — without it, the panel reading 「说话人0 → 张三」 while the list shows 李四 rows looks like a bug.

**Frontend (`index.html`):**
- State is a single `files[]` array; all UI is rebuilt by `renderFileList()` / `renderResults()` on every state change
- `timeupdate` on each `<audio>` drives playback-sync highlighting — the `playingSegEl` global tracks the currently highlighted row

**Recording frontend (`web/static/js/recorder.js`):** `initRecorder()` (wired in `main.js`) builds the bar lazily in JS — inserted between `#dropzone` and `#fileList`, plus a pulsing red tag in the transcript toolbar — rather than adding markup to `index.html`; the whole UI stays hidden when `GET /record/devices` reports `enabled:false`. The bar is a **single compact row** (device `<select>` + start/stop) with a **timer line that appears only while recording**; the level meter and the explanatory `<p>` hint that used to sit under it were removed by request. The device `<select>` is **always shown**, even when only one device was enumerated — hiding it at `length <= 1` made it impossible to confirm which mic was in use, which is the one thing worth checking before recording. Recording state lives in module scope, **not** in `state.js`. The timer comes from the server (`recorded_seconds`) so closing and reopening the browser resumes rather than restarts it; polling runs only while this client believes a recording is live. `syncRecorder()` aligns on meeting switch (`history:active`), `noteRecordingFrame(status, startedAt)` aligns on SSE frames (called from `transcribe-events.js`, whose `applyFileEvent` clears the timer on the *first* non-`recording` frame). The stop sequence `recording → pending → queued → processing → done` needs no special handling anywhere else: from `pending` onward it is an ordinary file. **`GET /history/{id}/record` still returns `level`** (`_recorder.level()` → `pcm_rms_tail`) — nothing in the UI reads it now, but it is kept so a level indicator can be reinstated without a server change.

## Key constraints

- The server is intentionally single-file and dependency-light; avoid introducing a JS bundler or frontend framework
- Model loading is blocking and happens once — do not move it inside the request handler
- `seg.start` / `seg.end` are in **seconds** (floats); the raw FunASR output is in **milliseconds** and is divided by 1000 in `app.py`
- Business logic that doesn't need the model lives in `services.py` so it stays unit-testable without a GPU/model load — keep new pure logic there, not inline in the route handlers
- The frontend Q&A panel reads `/chat` as an SSE stream; AI output is rendered as Markdown via `renderMarkdown`, which **escapes first (`escapeHtml`) then applies formatting** — it never injects raw model HTML, preserving the anti-injection guarantee
- Voiceprint matching is **advisory only**: it fills `speakerMap` (display names), never `seg.speaker`, and never overwrites a manual mapping. `speaker_matches` is transient (JSON return / SSE frame) and deliberately **not** stored in the history payload — do not add it to the payload whitelist. All SV audio IO stays in `sv_embed.py` so tests can stub it. Note it is **not** ffmpeg-free by choice: `load_audio_16k_mono` takes the soundfile fast path only for files that are already 16 k mono and otherwise shells out to ffmpeg, because the numpy `np.interp` resample it used to fall back on has no anti-alias filter. The 48 k capture made that path routine (every recording), so the aliasing was no longer hypothetical.
- **「录音停止后它就是一个普通文件」is the core invariant of the recording feature.** A stopped recording enters the payload as an ordinary file entry with an ordinary `audio_name`; `renderFileList` / `renderResults` / the player / export / summary / chat / history need **no recording-specific branches**. Anything that would break that — a second transcription route, a special display path, a separate storage location — is the wrong shape for this feature.
- Recording audio is written by ffmpeg **directly to disk**, past `audio_store.save_audio` and therefore past `AUDIO_MAX_BYTES`; `RECORD_MAX_BYTES` + the recorder's watchdog are what bound it. Recording audio, like uploaded audio, never enters SQLite (the payload holds references only), and `recording_target` carries the same path-traversal guard as `audio_path`.
- Recording is **server-side and unauthenticated** — the app has no auth and listens on `0.0.0.0`, so anyone who can reach port 8899 can start a hot mic on the host. `RECORD_ENABLED` is the master kill-switch; the deployment is documented as LAN-only. Do not widen this without saying so explicitly.
- The recording's `ffmpeg` subprocess needs `/dev/snd/*`, which belongs to the host's `audio` group — hence `SupplementaryGroups=audio` in `deploy/systemd/edgeminute.service.tpl`. Without that line capture fails at device-open. `RECORD_*` env vars are read from `.env` via `EnvironmentFile`, not hardcoded in the unit.
- `meta.attendees` is an array of `{name,unit,title}`; the panel renders a structured editor (add/remove rows). `normalizeMeta` is the single coercion boundary — every external `meta` (old saved sessions, `/import-agenda` results, undo/redo restore) passes through it, so legacy string-`attendees` / stray `host` are handled in one place. Keep new `meta` consumers going through `normalizeMeta` / `attendeeLines`.
- **`file_index` is never renumbered — this is the constraint that shapes per-card delete.** It is the array position, the route param, the SSE frame key, the `{index}_mic.wav` filename prefix, and the in-flight job identity all at once. Anything that shifts indices (a `splice`, a filtered rebuild, an "compact the list" cleanup) breaks all five together, and does so **quietly** because `audio_name` is what the read paths actually use — the audio still plays, from the wrong slot. Deletion therefore leaves a tombstone and preserves the index space; do not "tidy up" the array later.
