"""
密纪 EdgeMinute — 本地会议纪要服务（Minutes that never leave the room.）

会议不上云，纪要不外传：FunASR 语音转写 + 说话人分离 + 声纹识别，
全流程本地内网运行，音频与转写数据不出域。

用法: python3 app.py
访问: http://<host-ip>:80
"""
# 注解惰性求值：本模块在 Python 3.9 上运行，PEP 604 的 `X | None` 等联合注解在
# 运行期求值会报 TypeError；字符串化所有注解可统一兼容（也避免函数签名踩同样的坑）。
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import json
import os
import shutil
import subprocess
import tempfile
import time

# 历史备注：曾设 NUMBA_DISABLE_JIT=1 以规避 Jetson aarch64 上 numba/llvmlite 高版本
# JIT（LLVM RuntimeDyld）触发的 `isInt<33>` 重定位溢出 core dump。该崩溃现已通过
# 钉死 numba==0.56.4 / llvmlite==0.39.1（见 requirements.txt / install 脚本）根治，
# 故不再需要禁用 JIT——保留禁用反而会让 CAM++ 的热点函数退回解释执行、显著拖慢
# 长音频转写。切勿在未同步升级/验证 numba 版本前重新加回该开关。

import httpx
import jinja2
from docxtpl import DocxTemplate
from fastapi import Body, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess

import audio_store
import db
import contacts
import recorder
import sv_embed
import transcribe_jobs
from services import (
    FILE_DELETED,
    build_agenda_extract_messages,
    build_history_payload,
    build_llm_messages,
    build_recording_name,
    build_representative_segments,
    build_summary_messages,
    build_template_context,
    derive_meeting_status,
    env_float,
    env_int,
    extract_docx_text,
    extract_history_columns,
    group_by_date,
    is_deleted_file,
    list_capture_devices,
    pick_capture_device,
    match_speakers,
    parse_agenda_json,
    parse_summary_json,
    preserve_deleted_files,
    preserve_recording_file,
    raw_pcm_seconds,
    recording_raw_name,
    recording_wav_name,
    tombstone_file,
    validate_history_payload,
    wrap_raw_to_wav,
)

# ---------- 配置 ----------
#MODEL_ID = "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
MODEL_ID = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
DEVICE = os.environ.get("FUNASR_DEVICE", "cuda")

# SeACo 热词偏置强度：默认 1.0，调高可让热词更强地压过同音高频词（如“灵玑”vs“灵机”）。
# 只在有热词时透传给 model.generate(clas_scale=...)；不设或设为 1 时行为不变。
HOTWORD_CLAS_SCALE = env_float("HOTWORD_CLAS_SCALE", 1.0)

# 智能问答 LLM（OpenAI 兼容接口），可用环境变量覆盖
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "Qwen3-30B-A3B-Instruct")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")  # 多数本地部署无需密钥
LLM_TIMEOUT = env_float("LLM_TIMEOUT", 1800.0)
# 会议总结是非流式、单次长输出（多发言人×多要点），本地小算力下生成耗时远高于
# 普通问答。给它独立且充足的超时，避免因 .env 里 LLM_TIMEOUT 偏小而在生成中途
# 被客户端断开（llama-server 侧表现为 cancel task，app 侧回 502）。默认取两者较大值。
SUMMARY_TIMEOUT = max(env_float("SUMMARY_TIMEOUT", 1800.0), LLM_TIMEOUT)
CHAT_MAX_CONTEXT_CHARS = env_int("CHAT_MAX_CONTEXT_CHARS", 24000)

# 前端资源根目录（index.html / logo.svg / static/）
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")

# 会议纪要 Word 模板（用户可手动替换此文件）
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "templates", "minutes.docx")

# 会议历史 payload 大小上限（字节，文本部分；音频另计 AUDIO_MAX_BYTES）
HISTORY_PAYLOAD_MAX_BYTES = env_int("HISTORY_PAYLOAD_MAX_BYTES", 10 * 1024 * 1024)

# ---------- 声纹登记 + 自动匹配配置 ----------
# 独立 CAM++ SV 模型：登记提向量、转写后逐说话人切片提向量都用它。
# SV_ENABLED=0 可整体关闭声纹功能（不加载 SV 模型、匹配一律空、登记接口报 503）。
SV_ENABLED = os.environ.get("SV_ENABLED", "1") not in ("0", "false", "False", "")
SV_MODEL_ID = os.environ.get("SV_MODEL_ID", "iic/speech_campplus_sv_zh-cn_16k-common")
# 余弦相似度匹配阈值：>= 阈值才算命中（需按实测分布校准，0.65 为经验中间值）
VOICEPRINT_MATCH_THRESHOLD = env_float("VOICEPRINT_MATCH_THRESHOLD", 0.65)
# 每个说话人簇最多取几段最长语音拼成代表片段提向量（越多越稳但越慢）
VOICEPRINT_MAX_SEGMENTS = env_int("VOICEPRINT_MAX_SEGMENTS", 3)
# 单段短于此秒数则跳过（太短的语音提出的向量不可靠）
VOICEPRINT_MIN_SEG_SECONDS = env_float("VOICEPRINT_MIN_SEG_SECONDS", 0.5)
# 登记音频上传大小上限（字节）；提完向量即丢弃，不落盘
VOICEPRINT_SAMPLE_MAX_BYTES = env_int("VOICEPRINT_SAMPLE_MAX_BYTES", 20 * 1024 * 1024)

# ---------- 麦克风录音配置 ----------
# RECORD_ENABLED=0 可整体关闭录音（隐藏 UI、接口报 503），与 SV_ENABLED 同构。
# 注意：探测器 = 服务端能否枚举到采集设备 + ffmpeg 是否可用，二者任一不满足即视为关闭。
RECORD_ENABLED = os.environ.get("RECORD_ENABLED", "1") not in ("0", "false", "False", "")
# 指定默认录音设备（plughw 串，如 plughw:CARD=Device,DEV=0）；留空则取枚举到的第一个。
RECORD_DEVICE = os.environ.get("RECORD_DEVICE", "").strip()
# 录音大小上限（字节，由 audio_store.RECORD_MAX_BYTES 提供默认，见该模块说明）

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """服务生命周期：关闭时收尾在途录音。

    录音由 ffmpeg 子进程承担，进程随服务一起被 systemd 收掉。若不在这里主动停一次，
    ffmpeg 拿不到 SIGINT，最后一段写缓冲丢失、裸流也不会补 WAV 头；而补头本身还能让
    「服务重启」这一路径与「服务被 SIGKILL」区分开——前者能干净收尾，后者靠启动扫描
    兜底（见 _recover_interrupted_recordings）。shutdown 不提交转写（服务正在停，提了
    也跑不完，只会留下一堆 queued 被下次启动判成 error）。
    """
    yield
    try:
        await _recorder.shutdown()
    except Exception as _e:  # 关闭路径绝不因录音收尾失败而中断
        print(f"录音收尾跳过：{_e}")


app = FastAPI(title="密纪 EdgeMinute — 本地会议纪要服务", lifespan=_lifespan)


# 前端静态资源（ESM 模块 + CSS）。index.html 以模块方式引用 /static/js/main.js。
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(WEB_DIR, "static")),
    name="static",
)

print(f"正在加载模型（device={DEVICE}），请稍候...")
model = AutoModel(
    model=MODEL_ID,
    vad_model="fsmn-vad",
    # VAD 单段上限 30s：限制送进 Paraformer 的最长语音段，降低显存峰值
    vad_kwargs={"max_single_segment_time": 30000},
    punc_model="ct-punc",
    spk_model="cam++",
    device=DEVICE,
    disable_update=True,
)
print("模型加载完成，服务已就绪。")

# 独立声纹（SV）模型：登记与匹配都用它提 192 维向量。
# 关键：加载失败**不得阻断服务启动**——仅关闭声纹功能，转写/历史等一切照常。
if SV_ENABLED:
    try:
        print(f"正在加载声纹模型 {SV_MODEL_ID}（device={DEVICE}）...")
        _sv_model = AutoModel(model=SV_MODEL_ID, device=DEVICE, disable_update=True)
        sv_embed.set_model(_sv_model)
        print("声纹模型加载完成。")
    except Exception as _e:  # 加载失败：降级为“声纹功能不可用”，不影响主流程
        print(f"声纹模型加载失败，已禁用声纹功能：{_e}")
        sv_embed.set_model(None)
else:
    print("声纹功能已关闭（SV_ENABLED=0）。")
    sv_embed.set_model(None)

# 会议历史：建表 + 建音频目录 + 清理孤儿音频（删行成功但删目录失败的兜底）
db.init_db()
contacts.init_db()
audio_store.init_store()
try:
    _removed = audio_store.sweep_orphan_audio(db.all_meeting_ids())
    if _removed:
        print(f"已清理孤儿音频目录：{_removed}")
except Exception as _e:  # 清理失败不应阻断启动
    print(f"孤儿音频清理跳过：{_e}")


def _reset_stale_transcribing() -> int:
    """启动时复位残留的转写任务：进程重启后内存任务已丢失，DB 里却可能停在
    transcribing。把这些会议中 queued/processing 的文件标为 error（可重新转写），
    并按文件态重算会议级 status。返回受影响的会议数。

    注意 `recording` 不在此列：它是采集态而非推理态，由
    _recover_interrupted_recordings 先补 WAV 头再落到 pending（见 lifespan 的调用
    顺序）。若这里把 recording 一起标 error，崩溃恢复就会白丢一个可救的录音。
    """
    affected = 0
    for mid in db.ids_by_status("transcribing"):
        item = db.get_meeting(mid)
        if not item:
            continue
        payload = item.get("payload") or {}
        files = payload.get("files") or []
        changed = False
        for f in files:
            if f.get("status") in ("queued", "processing"):
                f["status"] = "error"
                f["error"] = "服务重启，转写已中断，请重新转写"
                changed = True
        if changed:
            payload["status"] = derive_meeting_status(files)
            cols = extract_history_columns(payload)
            db.update_payload(mid, payload, cols, _now_iso())
            affected += 1
    return affected


def _recover_interrupted_recordings() -> list[tuple[int, int]]:
    """启动扫描：把崩溃/强杀遗留的录音裸流补成 WAV，并复位文件态。

    这是「录裸流」这一选择的兑现点（见 services.wrap_raw_to_wav 的说明）：裸流任意
    长度的前缀都是合法音频，故 SIGKILL / 断电后仍能按实际大小补出可转写的 WAV，损失
    以 ffmpeg 未刷盘的写缓冲为界。三件事按文件逐一处理，互不影响：

    - 有裸流 → 补 WAV，置 pending，返回给调用方以便提交转写；
    - 无裸流（ffmpeg 一秒都没写成）→ 置 error，给用户看得懂的提示；
    - 会议已不存在（删了一半）→ 跳过，交给孤儿音频清理。
    返回 [(meeting_id, file_index)]，仅含成功补头、可转写的文件。
    """
    recovered: list[tuple[int, int]] = []
    root = audio_store.AUDIO_DIR
    if not os.path.isdir(root):
        return recovered
    for dirname in sorted(os.listdir(root)):
        if not dirname.isdigit():
            continue
        meeting_id = int(dirname)
        item = db.get_meeting(meeting_id)
        if item is None:
            continue
        payload = item.get("payload") or {}
        files = payload.get("files") or []
        changed = False
        for idx, f in enumerate(files):
            if f.get("status") != "recording":
                continue
            raw_name = recording_raw_name(idx)
            wav_name = recording_wav_name(idx)
            raw_path = os.path.join(root, dirname, raw_name)
            wav_path = os.path.join(root, dirname, wav_name)
            if os.path.isfile(raw_path):
                try:
                    data_bytes = wrap_raw_to_wav(raw_path, wav_path)
                except OSError as e:
                    f["status"] = "error"
                    f["error"] = f"录音收尾失败：{e}"
                    changed = True
                    continue
                f["status"] = "pending"
                f["audio_name"] = wav_name
                f["has_audio"] = True
                f["duration"] = round(raw_pcm_seconds(data_bytes), 1)
                f.pop("error", None)
                recovered.append((meeting_id, idx))
            else:
                f["status"] = "error"
                f["error"] = "录音中断，未产生音频"
            changed = True
        if changed:
            payload["status"] = derive_meeting_status(files)
            db.update_payload(meeting_id, payload, extract_history_columns(payload), _now_iso())
    return recovered


try:
    _reset = _reset_stale_transcribing()
    if _reset:
        print(f"已复位 {_reset} 个中断的转写会议（标为可重新转写）")
except Exception as _e:  # 复位失败不应阻断启动
    print(f"转写复位跳过：{_e}")

# 崩溃恢复须在 _reset_stale_transcribing 之后：后者会把 transcribing 会议里的
# queued/processing 标为 error，而录音残留是 recording 态（不在其列），由这里补头
# 后落到 pending。两者顺序调换也不会互相破坏，但先复位再补头能少写一次 DB。
try:
    _recovered = _recover_interrupted_recordings()
    if _recovered:
        print(f"已恢复 {len(_recovered)} 路中断的录音（补 WAV 头，可重新转写）")
except Exception as _e:  # 恢复失败不应阻断启动
    print(f"录音恢复跳过：{_e}")


# ---------- 麦克风录音：服务端采集 + 收尾落库 ----------
async def _persist_record_result(
    meeting_id: int, file_index: int, *, status: str, duration: float,
    error: str, raw_path: str, wav_path: str, hotword: str = "",
) -> None:
    """录音收尾后的落库回调（recorder.on_finalize）。

    把 WAV 引用与时长写回该文件条目，重算会议级状态，并按 status 决定是否提交转写：
    `pending`（正常停止 / 触顶自动停）→ 提交转写，与上传完成后自动转写的行为一致；
    `error`（进程异常退出但已救回音频）→ 不提交，把决定权留给用户，避免把一个已知
    残缺的录音直接送去转写。音频始终保留，用户可手动点「开始转写」。
    """
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return  # 会议已被删除：丢弃结果（音频目录随后由删除流程清掉）
    payload = item.get("payload") or {}
    files = payload.get("files") or []
    if file_index < 0 or file_index >= len(files):
        return
    f = files[file_index]
    # 槽位已被删除（墓碑）：这次收尾无处可写。删除路由只挡住「正在录」的槽位，而
    # stop() 一进来就把登记项摘掉了，因此「停止录音 → 收尾完成」这几秒里用户仍可能
    # 删掉这张卡；若不拦，收尾回调会把一个刚删掉的条目连音频引用一起写回去。
    # 音频随删除一起清掉，避免留下无引用的孤儿文件。
    if is_deleted_file(f):
        await run_in_threadpool(audio_store.delete_file_audio,
                                meeting_id, os.path.basename(wav_path))
        return
    f["status"] = "pending" if status == "pending" else "error"
    f["audio_name"] = os.path.basename(wav_path)
    f["has_audio"] = True
    if duration:
        f["duration"] = round(duration, 1)
    if error:
        f["error"] = error
    else:
        f.pop("error", None)
    payload["status"] = derive_meeting_status(files)
    await run_in_threadpool(db.update_payload, meeting_id, payload,
                            extract_history_columns(payload), _now_iso())
    _jobs.broadcast(meeting_id, {"type": "meeting", "status": payload["status"]})
    if f["status"] == "pending":
        _submit_record_transcribe(meeting_id, file_index, hotword)


def _submit_record_transcribe(meeting_id: int, file_index: int, hotword: str = "") -> None:
    """提交已收尾录音的整文件转写。

    刻意复用与上传路径完全相同的运行器与持久化回调（_run_asr_from_disk /
    _persist_transcribe_result）：这正是「录音停止后就是一个普通文件」这条不变式在
    代码上的落点——转写侧不知道、也不需要知道音频来自麦克风还是上传。
    """
    async def runner():
        return await _run_asr_from_disk(meeting_id, file_index, hotword)
    _jobs.submit(meeting_id, file_index, runner, _persist_transcribe_result)


# B2 后台转写任务管理器（进程内登记 + SSE 事件广播）。
# 必须早于下面的 _recorder 构造：RecorderManager 把 _jobs.broadcast 作为 emit 注入，
# 这是 import 期求值（不是调用期），定义顺序反了会在启动时直接 NameError。
_jobs = transcribe_jobs.TranscribeManager()


# 录音管理器：事件走 _jobs 的同一条 SSE 总线（broadcast 是公开注入点），
# 收尾落库走上面的回调。大小上限由 audio_store 提供（ffmpeg 直写，服务端不经手字节）。
_recorder = recorder.RecorderManager(
    emit=_jobs.broadcast,
    on_finalize=_persist_record_result,
    max_bytes=audio_store.RECORD_MAX_BYTES,
)


@app.get("/record/devices")
async def record_devices():
    """列出可用的采集设备，供前端渲染设备下拉与「录音」入口的可见性。

    enabled 由 RECORD_ENABLED 与「确实挑得出一个设备」共同决定——在开发机（无
    /proc/asound）上自然为 false，前端据此整块隐藏录音 UI，而不是显示一个点了必然
    失败的按钮。default_device 是建议默认项（RECORD_DEVICE 优先，其次 USB 卡）。

    注意启用判据用的是 picked 而不是 devices：显式配置了 RECORD_DEVICE 而枚举恰好
    失败时，pick_capture_device 会直接采信该串（用户能用 arecord 录就说明设备存在），
    此处必须与 /record/start 的判据一致，否则会出现「接口肯录音、UI 却不给入口」。
    """
    devices = list_capture_devices(RECORD_DEVICE)
    picked = pick_capture_device(devices, RECORD_DEVICE)
    return JSONResponse({
        "ok": True,
        "enabled": bool(RECORD_ENABLED and picked),
        "devices": devices,
        "default_device": (picked or {}).get("plughw", ""),
    })


@app.post("/history/{meeting_id}/record/start")
async def record_start(meeting_id: int, payload: dict = Body(default={})):
    """开始一路麦克风录音：分配文件槽、落盘、启动 ffmpeg 采集。返回 { ok, file_index }。

    文件条目在**开始时就写入 payload**（status=recording、audio_name 指向收尾后的
    .wav 名）：这样浏览器关掉再打开、甚至服务重启扫描，都能从服务端得知「这场会议正在
    录音」，而不是靠前端内存记着。音频文件此刻尚不存在——这是录音唯一的例外，由
    audio_store.recording_target 专门授权。
    """
    if not RECORD_ENABLED:
        return JSONResponse({"ok": False, "error": "录音功能已关闭"}, status_code=503)
    devices = list_capture_devices(RECORD_DEVICE)
    if not devices:
        return JSONResponse({"ok": False, "error": "未检测到可用的录音设备"}, status_code=503)

    device = str((payload or {}).get("device", "") or "").strip() or RECORD_DEVICE
    picked = pick_capture_device(devices, device)
    if picked is None:
        return JSONResponse({"ok": False, "error": "录音设备不可用"}, status_code=400)
    device = picked["plughw"]
    hotword = str((payload or {}).get("hotword", "") or "").strip()

    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    if _recorder.is_recording(meeting_id):
        return JSONResponse({"ok": False, "error": "该会议正在录音中"}, status_code=409)

    payload_obj = item.get("payload") or {}
    files = list(payload_obj.get("files") or [])
    file_index = len(files)
    try:
        raw_path, wav_path = await run_in_threadpool(
            audio_store.recording_target, meeting_id, recording_raw_name(file_index)
        )
    except (ValueError, OSError) as e:
        return JSONResponse({"ok": False, "error": f"录音目录不可用：{e}"}, status_code=500)

    files.append({
        "id": f"rec{file_index}",
        "name": build_recording_name(_now_iso()),
        "duration": None,
        "elapsed": None,
        "audio_name": recording_wav_name(file_index),
        "has_audio": True,
        "status": "recording",
        "segments": [],
    })
    payload_obj["files"] = files
    payload_obj["status"] = derive_meeting_status(files)
    await run_in_threadpool(db.update_payload, meeting_id, payload_obj,
                            extract_history_columns(payload_obj), _now_iso())

    try:
        started = _recorder.start(meeting_id, file_index, device, raw_path, wav_path, hotword)
    except Exception as e:  # ffmpeg 缺失 / 设备打不开：回滚刚建的文件条目，不留半成品
        payload_obj["files"] = payload_obj["files"][:file_index]
        payload_obj["status"] = derive_meeting_status(payload_obj["files"])
        await run_in_threadpool(db.update_payload, meeting_id, payload_obj,
                                extract_history_columns(payload_obj), _now_iso())
        return JSONResponse({"ok": False, "error": f"无法启动录音：{e}"}, status_code=500)
    if not started:
        return JSONResponse({"ok": False, "error": "该会议正在录音中"}, status_code=409)

    _jobs.broadcast(meeting_id, {"type": "meeting", "status": payload_obj["status"]})
    return JSONResponse({"ok": True, "file_index": file_index,
                         "device": device, "started_at": _recorder.active_started_at(meeting_id).get(file_index)})


@app.post("/history/{meeting_id}/record/stop")
async def record_stop(meeting_id: int):
    """停止录音：收尾补 WAV 头 → 落库 → 自动提交整文件转写。幂等（未在录返回 404）。"""
    if not await _recorder.stop(meeting_id):
        return JSONResponse({"ok": False, "error": "该会议当前没有录音"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/history/{meeting_id}/record")
async def record_status(meeting_id: int):
    """查询某会议的录音运行态。给**没有 SSE 连接**的场景兜底（浏览器重开/换设备）：
    前端启动时拉一次即可决定按钮态与计时起点，计时不归零。"""
    started = _recorder.active_started_at(meeting_id)
    file_index = _recorder.active_file_index(meeting_id)
    return JSONResponse({
        "ok": True,
        "recording": file_index is not None,
        "file_index": file_index,
        "started_at": started.get(file_index) if file_index is not None else None,
        "recorded_seconds": round(_recorder.recorded_seconds(meeting_id), 1),
        "level": round(_recorder.level(meeting_id), 4),
    })


# 推理串行锁：模型/显存不支持多路并发推理，用锁把多次 /transcribe 串行化。
# 关键在于推理本身跑在线程池（run_in_threadpool），不占事件循环——
# 因此转写进行中，/import-agenda、/chat 等其它请求仍能并行响应。
#
# 惰性创建：不在 import 期实例化。Python 3.10+ 的 asyncio.Lock 在构造时即绑定
# 「当时的事件循环」，而 import 阶段 uvicorn 的正式 loop 尚未启动，会把锁绑到错误
# 的 loop；等到并发争用真正需要 await 挂起时，就会抛
# 「got Future attached to a different loop」。改为首次使用时在运行中的 loop 内创建。
_infer_lock: asyncio.Lock | None = None   # 惰性创建，见 _get_infer_lock


def _get_infer_lock() -> asyncio.Lock:
    """返回推理串行锁，首次调用时在当前（uvicorn）事件循环内惰性创建。

    所有调用点都在请求/后台任务协程内执行，此时已处于 uvicorn 的 loop，
    因此锁被绑到正确的 loop；单进程单 loop，无需额外同步。
    """
    global _infer_lock
    if _infer_lock is None:
        _infer_lock = asyncio.Lock()
    return _infer_lock


# ASR 前音频预处理滤波链（识别前统一跑）：
#   highpass=f=80        去掉 80Hz 以下低频隆隆声（空调/桌面震动/直流偏置）
#   afftdn=nr=12         FFT 频域降噪，压制稳态背景噪声（12dB，偏保守不伤语音）
#   acompressor          动态压缩，拉近远近发言人音量差，压制突发爆音
#   loudnorm             EBU R128 响度归一化到 -16 LUFS，避免整体过轻/过响
# 这些参数直接并入既有的转 16k/单声道那一步，不新增处理阶段。
_ASR_AUDIO_FILTER = (
    "highpass=f=80,"
    "afftdn=nr=12,"
    "acompressor=threshold=-20dB:ratio=3:attack=5:release=50,"
    "loudnorm=I=-16:TP=-1.5:LRA=11"
)


def _ensure_audio_input(file_path: str) -> "tuple[str, bool]":
    """统一预处理：所有输入都经 ffmpeg 转 16k 单声道 WAV 并跑降噪/响度归一化滤波，
    返回 (路径, 是否为临时文件)。ffmpeg 缺失或处理失败时降级为原文件直送
    （返回 (原路径, False)），保证音频预处理永不打断转写。"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp_path = tmp.name
    tmp.close()
    try:
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-i", file_path,
            "-af", _ASR_AUDIO_FILTER,
            "-f", "wav", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", "16000",
            "-y", tmp_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(stderr or f"ffmpeg 退出码 {proc.returncode}")
        return tmp_path, True
    except Exception as e:
        # 降级：清掉半成品临时文件，原文件直送 ASR（视频文件此时可能无法解码，
        # 但保持与旧行为一致——预处理是增强项，不应成为新的失败点）。
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        print(f"[ASR] 音频预处理失败，降级为原文件直送：{e}")
        return file_path, False


def _run_asr(tmp_path: str, hotword: str):
    """同步执行一次 ASR 推理（在工作线程中调用，不得触碰事件循环）。"""
    audio_path, is_temp = _ensure_audio_input(tmp_path)
    try:
        hw = hotword.strip()
        # 诊断：确认转写实际收到的热词字符串（SeACo 期望空格分隔的单行文本）
        print(f"[ASR] hotword={hw!r}  clas_scale={HOTWORD_CLAS_SCALE}  input={os.path.basename(tmp_path)}")
        # 长音频稳定性：动态 batch 从默认 300s 降到 60s，是显存 OOM 的头号控制项；
        # batch_size_threshold_s 让超过 50s 的 VAD 段强制单条推理，避免长段撑爆显存
        kwargs = dict(
            input=audio_path,
            hotword=hw,
            batch_size_s=150,
            batch_size_threshold_s=50,
        )
        # 仅在确有热词时透传 clas_scale，避免无热词场景引入无谓参数
        if hw:
            kwargs["clas_scale"] = HOTWORD_CLAS_SCALE
        return model.generate(**kwargs)
    finally:
        if is_temp:
            os.remove(audio_path)


def _compute_speaker_matches(audio_path: str, sentence_info: list) -> dict:
    """同步计算说话人→登记人物的自动匹配（在工作线程中调用）。

    流程：按 spk 分组取最长段(纯函数) → 读整段音频并重采样 16k → 逐 spk
    切片拼接过 SV 模型提向量 → 与声纹库余弦匹配。返回 {spk(str): {name, score}}，
    仅含命中项；未命中/无库/功能关闭/任何异常 → 返回已得到的部分或 {}。

    该函数**只读**音频、**不写** seg.speaker——匹配结果交给前端填充 speakerMap，
    保持“聚类编号”这一原始事实不被覆盖。
    """
    if not sv_embed.is_enabled():
        return {}
    enrolled = contacts.get_all_embeddings()
    if not enrolled:
        return {}
    reps = build_representative_segments(
        sentence_info, VOICEPRINT_MAX_SEGMENTS, VOICEPRINT_MIN_SEG_SECONDS
    )
    if not reps:
        return {}
    try:
        audio = sv_embed.load_audio_16k_mono(audio_path)
    except Exception as e:
        print(f"[声纹匹配] 读音频失败，跳过匹配：{e}")
        return {}

    cluster_embeddings: dict = {}
    for spk, spans in reps.items():
        try:
            clip = sv_embed.slice_concat(audio, spans, sv_embed.TARGET_SR)
            if getattr(clip, "size", len(clip)) <= 0:
                continue
            cluster_embeddings[str(spk)] = sv_embed.embed_array(clip, sv_embed.TARGET_SR)
        except Exception as e:  # 单个簇失败不影响其它簇
            print(f"[声纹匹配] spk={spk} 提向量失败，跳过：{e}")
            continue
    if not cluster_embeddings:
        return {}
    # 诊断日志：逐簇打印“与每个登记人的相似度”，便于定位是“提向量失败(异常被吞)”
    # 还是“分数没到阈值”。未命中时也能看到最像谁、差多少，据此校准阈值。
    from services import cosine_similarity  # 局部导入，避免顶部 import 列表膨胀
    print(f"[声纹匹配] 登记库 {len(enrolled)} 人，阈值={VOICEPRINT_MATCH_THRESHOLD}，"
          f"待匹配簇={list(cluster_embeddings.keys())}")
    for spk, emb in cluster_embeddings.items():
        scores = sorted(
            ((cosine_similarity(emb, p.get("embedding")), p.get("name")) for p in enrolled),
            reverse=True,
        )
        detail = "，".join(f"{name}={score:.3f}" for score, name in scores[:5])
        print(f"[声纹匹配] spk={spk} 相似度：{detail or '(库为空)'}")
    return match_speakers(cluster_embeddings, enrolled, VOICEPRINT_MATCH_THRESHOLD)


@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/logo.svg")
async def logo():
    return FileResponse(os.path.join(WEB_DIR, "logo.svg"))


@app.get("/favicon.svg")
async def favicon():
    """站点图标（浏览器标签页）。与 /logo.svg 同样的静态返回方式。
    浏览器还会自动请求 /favicon.ico；此处不提供，让它 404 即可——index.html 里的
    <link rel="icon"> 已明确指向本路由，浏览器不会再退回那个默认路径。"""
    return FileResponse(os.path.join(WEB_DIR, "favicon.svg"), media_type="image/svg+xml")


HOTWORDS_PATH = os.path.join(os.path.dirname(__file__), "hotwords.txt")
HOTWORDS_MAX_BYTES = 64 * 1024  # 热词文本上限，防止超大写入


@app.get("/hotwords")
async def get_hotwords():
    try:
        if os.path.exists(HOTWORDS_PATH):
            with open(HOTWORDS_PATH, "r", encoding="utf-8") as f:
                return JSONResponse({"ok": True, "hotword": f.read()})
        return JSONResponse({"ok": True, "hotword": ""})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/hotwords")
async def save_hotwords(payload: dict = Body(...)):
    text = payload.get("hotword", "")
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "hotword 必须是字符串"}, status_code=400)
    if len(text.encode("utf-8")) > HOTWORDS_MAX_BYTES:
        return JSONResponse({"ok": False, "error": "热词内容过长"}, status_code=413)
    try:
        with open(HOTWORDS_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------- 通讯录（联系人 + 声纹）登记 + 管理 ----------
# 「通讯录」对外统一命名；联系人可选带声纹（sample_count>0）。声纹匹配内部机制
# 仍保留 voiceprint/SV 命名（sv_embed、VOICEPRINT_* 参数），语义准确。
def _extract_sample_embedding(tmp_path: str) -> list:
    """在工作线程中对登记样本文件提声纹向量（同步，抢推理锁的调用方保证串行）。"""
    return sv_embed.embed_file(tmp_path)


async def _embed_uploaded_sample(file: UploadFile) -> list:
    """把上传的登记音频落到临时文件 → 抢锁提向量 → **删临时文件**（提完即丢，不持久化）。

    返回 L2 归一化后的向量 list。任何失败向上抛，由路由转成 4xx/5xx。
    """
    data = await file.read()
    if len(data) > VOICEPRINT_SAMPLE_MAX_BYTES:
        raise ValueError("音频文件过大")
    if not data:
        raise ValueError("音频为空")
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        async with _get_infer_lock():
            return await run_in_threadpool(_extract_sample_embedding, tmp_path)
    finally:
        try:
            os.remove(tmp_path)  # 提完向量即丢弃音频（用户既定：不保留登记音频）
        except OSError:
            pass


@app.get("/contacts")
async def contacts_list():
    """列出全部联系人（不含向量）。返回 { ok, enabled, people, threshold }。

    每人补 has_voice=(sample_count>0)：区分「已录声纹」与「仅通讯录」。enabled 反映
    声纹功能是否可用——为 False 时仍可管理无声纹联系人，只是不能登记/匹配声纹。
    """
    try:
        people = await run_in_threadpool(contacts.list_people)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"查询失败：{e}"}, status_code=500)
    for p in people:
        p["has_voice"] = (p.get("sample_count") or 0) > 0
    return JSONResponse({
        "ok": True,
        "enabled": sv_embed.is_enabled(),
        "threshold": VOICEPRINT_MATCH_THRESHOLD,
        "people": people,
    })


@app.post("/contacts")
async def contacts_add(payload: dict = Body(...)):
    """新增**无声纹**联系人（仅姓名/单位/职务，无需音频）。body: { name, unit?, title? }。

    返回 { ok, id }。声纹功能是否启用**不影响**此接口——通讯录本身不依赖 SV 模型。
    判重（同名+同单位）由前端提示、不在此拦截（用户既定：不强制拦截）。
    """
    name = str(payload.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "姓名不能为空"}, status_code=400)
    unit = str(payload.get("unit", "") or "").strip()
    title = str(payload.get("title", "") or "").strip()
    try:
        pid = await run_in_threadpool(contacts.add_contact, name, _now_iso(), unit, title)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"入库失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "id": pid})


@app.post("/contacts/voice")
async def contacts_enroll_voice(
    file: UploadFile = File(...),
    name: str = Form(...),
    unit: str = Form(""),
    title: str = Form(""),
):
    """登记带声纹的联系人：上传一段干净单人语音 + 姓名(可选单位/职务)，提向量入库后丢弃音频。

    返回 { ok, id, sample_count }。声纹功能未启用 → 503。
    """
    if not sv_embed.is_enabled():
        return JSONResponse({"ok": False, "error": "声纹功能未启用"}, status_code=503)
    name = (name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "姓名不能为空"}, status_code=400)
    try:
        embedding = await _embed_uploaded_sample(file)
    except ValueError as e:
        code = 413 if "过大" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"声纹提取失败：{e}"}, status_code=500)
    try:
        pid = await run_in_threadpool(
            contacts.enroll_person, name, embedding, _now_iso(),
            (unit or "").strip(), (title or "").strip(),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"入库失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "id": pid, "sample_count": 1})


@app.post("/contacts/{contact_id}/samples")
async def contacts_add_sample(contact_id: int, file: UploadFile = File(...)):
    """为既有联系人追加一条声纹样本（首次即为其录声纹，或多录几段更稳）。返回 { ok, sample_id }。"""
    if not sv_embed.is_enabled():
        return JSONResponse({"ok": False, "error": "声纹功能未启用"}, status_code=503)
    # 先做廉价的存在性校验，避免对不存在的联系人白跑一次 GPU 提取（且提取会独占 _infer_lock）
    person = await run_in_threadpool(contacts.get_person, contact_id)
    if person is None:
        return JSONResponse({"ok": False, "error": "联系人不存在"}, status_code=404)
    try:
        embedding = await _embed_uploaded_sample(file)
    except ValueError as e:
        code = 413 if "过大" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"声纹提取失败：{e}"}, status_code=500)
    try:
        sid = await run_in_threadpool(
            contacts.add_sample, contact_id, embedding, _now_iso()
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"入库失败：{e}"}, status_code=500)
    if sid is None:
        return JSONResponse({"ok": False, "error": "联系人不存在"}, status_code=404)
    return JSONResponse({"ok": True, "sample_id": sid})


@app.get("/contacts/{contact_id}/samples")
async def contacts_list_samples(contact_id: int):
    """列出某联系人的声纹样本元信息（不含向量）。返回 { ok, samples }。"""
    try:
        person = await run_in_threadpool(contacts.get_person, contact_id)
        if person is None:
            return JSONResponse({"ok": False, "error": "联系人不存在"}, status_code=404)
        samples = await run_in_threadpool(contacts.list_samples, contact_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"查询失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "samples": samples})


@app.delete("/contacts/{contact_id}/samples/{sample_id}")
async def contacts_delete_sample(contact_id: int, sample_id: int):
    """删除某条声纹样本并重算该联系人平均向量。返回 { ok }。"""
    try:
        ok = await run_in_threadpool(contacts.delete_sample, sample_id, _now_iso())
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"删除失败：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "样本不存在"}, status_code=404)
    return JSONResponse({"ok": True})


@app.patch("/contacts/{contact_id}")
async def contacts_rename(contact_id: int, payload: dict = Body(...)):
    """改联系人姓名（可选单位/职务）。body: { name, unit?, title? }。返回 { ok }。"""
    name = str(payload.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "姓名不能为空"}, status_code=400)
    unit = payload.get("unit")
    title = payload.get("title")
    try:
        ok = await run_in_threadpool(
            contacts.rename, contact_id, name, _now_iso(),
            None if unit is None else str(unit).strip(),
            None if title is None else str(title).strip(),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"重命名失败：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "联系人不存在"}, status_code=404)
    return JSONResponse({"ok": True})


@app.delete("/contacts/{contact_id}")
async def contacts_delete(contact_id: int):
    """删除联系人及其全部声纹样本。返回 { ok }。"""
    try:
        ok = await run_in_threadpool(contacts.delete_person, contact_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"删除失败：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "联系人不存在"}, status_code=404)
    return JSONResponse({"ok": True})


# ---------- 转写行标注声纹：截取段音频 → 提向量 → 登记 ----------
@app.post("/history/{meeting_id}/audio/{file_index}/voiceprint")
async def annotate_voiceprint(meeting_id: int, file_index: int, payload: dict = Body(...)):
    """从会议音频中截取指定时间段，提取声纹向量，登记到声纹库。

    请求体: { start, end, person_name }
    - start/end: 秒（float），该段的起止时间
    - person_name: 人物姓名（必填）。已存在则追加样本，不存在则新建人物。

    返回: { ok, person_id, person_name, sample_count, is_new }
    """
    if not sv_embed.is_enabled():
        return JSONResponse({"ok": False, "error": "声纹功能未启用"}, status_code=503)

    # 校验 meeting 与音频文件存在
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    files = (item.get("payload") or {}).get("files") or []
    if file_index < 0 or file_index >= len(files):
        return JSONResponse({"ok": False, "error": "文件索引越界"}, status_code=404)
    audio_name = files[file_index].get("audio_name") or ""
    path = await run_in_threadpool(audio_store.audio_path, meeting_id, audio_name)
    if not path:
        return JSONResponse({"ok": False, "error": "音频不存在"}, status_code=400)

    # 校验时间范围
    start = float(payload.get("start", -1))
    end = float(payload.get("end", -1))
    if not (0 <= start < end):
        return JSONResponse({"ok": False, "error": "时间范围无效（需 0 ≤ start < end）"}, status_code=400)
    person_name = str(payload.get("person_name", "") or "").strip()
    if not person_name:
        return JSONResponse({"ok": False, "error": "姓名不能为空"}, status_code=400)

    # 提取声纹（抢推理锁，工作线程中执行）
    def _extract():
        audio = sv_embed.load_audio_16k_mono(path)
        clip = sv_embed.slice_concat(audio, [(start, end)], sv_embed.TARGET_SR)
        if getattr(clip, "size", len(clip)) <= 0:
            raise ValueError("截取的音频段为空（时间范围可能过短或越界）")
        return sv_embed.embed_array(clip, sv_embed.TARGET_SR)

    try:
        async with _get_infer_lock():
            embedding = await run_in_threadpool(_extract)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"声纹提取失败：{e}"}, status_code=500)

    # 查找或创建人物 + 追加样本
    now = _now_iso()
    existing = await run_in_threadpool(contacts.find_person_by_name, person_name)
    if existing:
        sid = await run_in_threadpool(contacts.add_sample, existing["id"], embedding, now)
        if sid is None:
            return JSONResponse({"ok": False, "error": "人物不存在"}, status_code=404)
        return JSONResponse({
            "ok": True,
            "person_id": existing["id"],
            "person_name": existing["name"],
            "sample_count": existing["sample_count"] + 1,
            "is_new": False,
        })
    else:
        pid = await run_in_threadpool(contacts.enroll_person, person_name, embedding, now)
        return JSONResponse({
            "ok": True,
            "person_id": pid,
            "person_name": person_name,
            "sample_count": 1,
            "is_new": True,
        })


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...), hotword: str = ""):
    suffix = os.path.splitext(file.filename or "")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        start_time = time.time()
        # 推理放到线程池执行，避免阻塞事件循环（否则长音频转写期间
        # /import-agenda、/chat 等请求全部被卡住，直到转写结束才一起返回）；
        # 用 _infer_lock 保证多次转写串行，防止并发推理挤爆显存。
        async with _get_infer_lock():
            result = await run_in_threadpool(_run_asr, tmp_path, hotword)
            sentence_info = result[0].get("sentence_info", [])
            # 声纹自动匹配：紧接 ASR、仍持推理锁时执行（同样吃 GPU，需串行）。
            # 失败不影响转写返回——helper 内已吞异常返回 {}。
            speaker_matches = await run_in_threadpool(
                _compute_speaker_matches, tmp_path, sentence_info
            )
        elapsed = time.time() - start_time

        segments = []
        for seg in sentence_info:
            segments.append({
                "start": round(seg["start"] / 1000, 1),
                "end": round(seg["end"] / 1000, 1),
                "speaker": seg.get("spk", "?"),
                "text": rich_transcription_postprocess(seg["text"]),
            })

        return JSONResponse({
            "ok": True,
            "elapsed_seconds": round(elapsed, 1),
            "segments": segments,
            "speaker_matches": speaker_matches,
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        os.remove(tmp_path)


# ---------- 会议纪要模板导出 ----------
@app.post("/export/docx-template")
async def export_docx_template(payload: dict = Body(...)):
    """按 templates/minutes.docx 模板渲染会议纪要，返回填充后的 .docx。

    请求体: { meta, segments, options, summary? }
    - segments: [{start,end,speaker,text}]（seconds）
    - options: { mergeSpeaker, timestamp, speaker, speakerMap }
    - summary: { overview, speakers:[{speaker,points}], resolutions }
      （会议总结，可选；概要/各发言人观点/决议段落）
    """
    if not os.path.exists(TEMPLATE_PATH):
        return JSONResponse(
            {"ok": False, "error": f"模板文件不存在：{TEMPLATE_PATH}"},
            status_code=500,
        )
    meta = payload.get("meta") or {}
    segments = payload.get("segments") or []
    options = payload.get("options") or {}
    summary = payload.get("summary") or {}
    try:
        ctx = build_template_context(meta, segments, options, summary)
        tpl = DocxTemplate(TEMPLATE_PATH)
        # autoescape：转义值中的 < > &，避免转写文本里的特殊字符破坏文档 XML
        tpl.render(ctx, jinja_env=jinja2.Environment(autoescape=True))
        buf = io.BytesIO()
        tpl.save(buf)
        buf.seek(0)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"模板渲染失败：{e}"}, status_code=500)

    headers = {"Content-Disposition": 'attachment; filename="minutes.docx"'}
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


# ---------- 议程导入（上传 Word → LLM 提取会议信息）----------
AGENDA_MAX_BYTES = 5 * 1024 * 1024  # 上传 Word 文件大小上限 5MB


@app.post("/import-agenda")
async def import_agenda(file: UploadFile = File(...)):
    """上传会议议程 Word(.docx)，调用 LLM 提取会议信息，返回 { ok, meta }。

    meta: {title, date, place, agenda, attendees:[{name,unit,title}]}
    """
    filename = file.filename or ""
    if not filename.lower().endswith(".docx"):
        return JSONResponse(
            {"ok": False, "error": "仅支持 .docx 格式（旧版 .doc 请先另存为 .docx）"},
            status_code=400,
        )
    data = await file.read()
    if len(data) > AGENDA_MAX_BYTES:
        return JSONResponse({"ok": False, "error": "文件过大（上限 5MB）"}, status_code=413)

    try:
        text = extract_docx_text(data)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    body = {
        "model": LLM_MODEL,
        "messages": build_agenda_extract_messages(text),
        "temperature": 0.1,  # 低温：抑制 JSON 漂移
        "max_tokens": 1500,
        "stream": False,
    }
    url = f"{LLM_BASE_URL}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            r = await client.post(url, json=body, headers=_llm_headers())
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return JSONResponse({"ok": False, "error": "LLM 返回格式异常"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"LLM 请求失败：{e}"}, status_code=502)

    try:
        meta = parse_agenda_json(content)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    return JSONResponse({"ok": True, "meta": meta})


# ---------- 智能问答（转发 OpenAI 兼容 LLM）----------
def _llm_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"
    return headers


@app.post("/summarize")
async def summarize(payload: dict = Body(...)):
    """会议总结：非流式调用 LLM，返回 { ok, summary:{overview, speakers[], resolutions} }。

    请求体: { meta, segments, speakerMap }
    - segments: [{start,end,speaker,text}]（seconds）
    - speakerMap: {"0": "张三"} 发言人显示名映射（可选）
    错误处理与 /import-agenda 一致：LLM 异常 502，解析失败 400。
    """
    meta = payload.get("meta") or {}
    segments = payload.get("segments") or []
    speaker_map = payload.get("speakerMap") or {}
    if not segments:
        return JSONResponse({"ok": False, "error": "没有可总结的转写内容"}, status_code=400)

    body = {
        "model": LLM_MODEL,
        "messages": build_summary_messages(
            meta, segments, budget=CHAT_MAX_CONTEXT_CHARS, speaker_map=speaker_map
        ),
        "temperature": 0.2,  # 低温：抑制 JSON 漂移，保持总结稳定
        # 详实总结（多发言人、每人多要点）输出较长，给足空间避免 JSON 被截断解析失败
        "max_tokens": 4096,
        "stream": False,
    }
    url = f"{LLM_BASE_URL}/v1/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=SUMMARY_TIMEOUT) as client:
            r = await client.post(url, json=body, headers=_llm_headers())
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return JSONResponse({"ok": False, "error": "LLM 返回格式异常"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"LLM 请求失败：{e}"}, status_code=502)

    try:
        summary = parse_summary_json(content)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    return JSONResponse({"ok": True, "summary": summary})


@app.post("/chat")
async def chat(payload: dict = Body(...)):
    """针对会议内容的问答。stream=True 时以 SSE 流式透传 LLM 增量输出。"""
    try:
        messages = build_llm_messages(payload, budget=CHAT_MAX_CONTEXT_CHARS)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"请求数据无效：{e}"}, status_code=400)
    stream = bool(payload.get("stream", True))
    # 采样参数夹逼到合理范围，避免客户端传入越界值直达 LLM
    temperature = min(2.0, max(0.0, float(payload.get("temperature", 0.7) or 0.7)))
    max_tokens = min(8192, max(1, int(payload.get("max_tokens", 4096) or 4096)))
    body = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    url = f"{LLM_BASE_URL}/v1/chat/completions"

    if not stream:
        try:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
                r = await client.post(url, json=body, headers=_llm_headers())
                r.raise_for_status()
                data = r.json()
            content = data["choices"][0]["message"]["content"]
            return JSONResponse({"ok": True, "content": content})
        except (KeyError, IndexError, TypeError):
            return JSONResponse({"ok": False, "error": "LLM 返回格式异常"}, status_code=502)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"LLM 请求失败：{e}"}, status_code=502)

    async def event_stream():
        # 用显式 try/finally 保证：客户端断开时 StreamingResponse 关闭本生成器，
        # httpx 的 stream/client 上下文随之退出，及时释放到 LLM 的上游连接。
        client = httpx.AsyncClient(timeout=LLM_TIMEOUT)
        try:
            async with client.stream("POST", url, json=body, headers=_llm_headers()) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")[:300]
                    yield _sse_error(f"LLM 返回 {r.status_code}：{detail}")
                    return
                # 原样透传上游 SSE（OpenAI 兼容格式：data: {...}\n\n ... data: [DONE]）
                async for line in r.aiter_lines():
                    yield (line + "\n") if line else "\n"
        except Exception as e:
            # 不吞 CancelledError/GeneratorExit（它们不继承 Exception），
            # 客户端断开时会正常向上传播并触发 finally 清理。
            yield _sse_error(f"LLM 连接失败：{e}")
        finally:
            await client.aclose()

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse_error(msg: str) -> str:
    return "data: " + json.dumps({"error": msg}, ensure_ascii=False) + "\n\n"


# ---------- 会议历史记录 ----------
def _now_iso() -> str:
    """当前时间 ISO8601（本地时区，秒级）。"""
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# 音频回放的 MIME 推断（按扩展名，交给浏览器 <audio>）
_AUDIO_MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".aac": "audio/aac",
    ".webm": "audio/webm", ".opus": "audio/opus", ".wma": "audio/x-ms-wma",
    ".mp4": "audio/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
}


def _parse_range(range_header: str, file_size: int):
    """解析单段 bytes= Range 头，返回 (start, end)（含端）或 None（不满足/无效）。

    starlette 0.35 的 FileResponse 不支持 Range，<audio> 拖动进度条又依赖 206，
    故在音频端点自行实现单段范围响应（多段 Range 极罕见，此处不支持并回退整段）。
    """
    if not range_header or not range_header.strip().lower().startswith("bytes="):
        return None
    spec = range_header.split("=", 1)[1].strip()
    if "," in spec:  # 多段 Range 不支持，回退整段（返回 None）
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # 后缀形式 bytes=-N：最后 N 字节
            n = int(end_s)
            if n <= 0:
                return None
            start = max(0, file_size - n)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
    except ValueError:
        return None
    if start > end or start >= file_size:
        return None
    return start, min(end, file_size - 1)


def _audio_response(path: str, media_type: str, range_header: str):
    """按需返回整段(200)或范围(206)音频响应，支持 <audio> 进度条拖动的 Range 请求。"""
    file_size = os.path.getsize(path)
    rng = _parse_range(range_header or "", file_size)
    if rng is None:
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"}
        )
    start, end = rng
    length = end - start + 1

    def _iter():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(length),
    }
    return StreamingResponse(_iter(), status_code=206, media_type=media_type, headers=headers)


@app.post("/history")
async def history_create(request: Request):
    """创建一条会议历史（multipart）：payload(JSON 字符串) + audio_0/audio_1/...。

    音频与 payload.files 顺序对应，逐个落盘并回填 audio_name/has_audio，随后
    更新该行 payload。返回 { ok, id, created_at }。这是自动保存“首次创建”路径。
    """
    try:
        form = await request.form()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"表单解析失败：{e}"}, status_code=400)

    raw_payload = form.get("payload")
    if not raw_payload:
        return JSONResponse({"ok": False, "error": "缺少 payload 字段"}, status_code=400)
    try:
        payload = validate_history_payload(raw_payload, max_bytes=HISTORY_PAYLOAD_MAX_BYTES)
    except ValueError as e:
        code = 413 if "过大" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)

    now = _now_iso()
    try:
        # 先建行拿到 id（音频目录以 id 命名），再逐个存音频并回填引用
        cols = extract_history_columns(payload)
        meeting_id = await run_in_threadpool(db.insert_meeting, payload, cols, now)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"写入失败：{e}"}, status_code=500)

    try:
        files = payload.get("files") or []
        for idx, f in enumerate(files):
            up = form.get(f"audio_{idx}")
            if up is None or not hasattr(up, "file"):
                f["audio_name"] = ""
                f["has_audio"] = False
                continue
            audio_name = await run_in_threadpool(
                audio_store.save_audio, meeting_id, idx, up.file, up.filename or f.get("name", "")
            )
            f["audio_name"] = audio_name
            f["has_audio"] = True
        # 回填音频引用后更新该行 payload
        await run_in_threadpool(db.update_payload, meeting_id, payload, cols, now)
    except ValueError as e:  # 音频超限等
        # 回滚：删行 + 删已写音频，避免半成品记录
        await run_in_threadpool(audio_store.delete_meeting_audio, meeting_id)
        await run_in_threadpool(db.delete_meeting, meeting_id)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=413)
    except Exception as e:
        await run_in_threadpool(audio_store.delete_meeting_audio, meeting_id)
        await run_in_threadpool(db.delete_meeting, meeting_id)
        return JSONResponse({"ok": False, "error": f"音频保存失败：{e}"}, status_code=500)

    return JSONResponse({"ok": True, "id": meeting_id, "created_at": now})


@app.post("/history/{meeting_id}/audio/{file_index}")
async def history_append_audio(meeting_id: int, file_index: int, request: Request):
    """为已存在会议的某个文件补传音频（multipart 字段名 audio）。

    用于“首次建档后又有文件转写完成”的场景：建档走 POST /history（含当时已完成
    文件的音频），随后新完成的文件通过本接口补传，避免只走 JSON PUT 而丢失音频。
    落盘后回填该文件的 audio_name/has_audio 并重写行。返回 { ok, audio_name }。
    """
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    payload = item.get("payload") or {}
    files = payload.get("files") or []
    if file_index < 0 or file_index >= len(files):
        return JSONResponse({"ok": False, "error": "文件索引越界"}, status_code=404)

    try:
        form = await request.form()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"表单解析失败：{e}"}, status_code=400)
    up = form.get("audio")
    if up is None or not hasattr(up, "file"):
        return JSONResponse({"ok": False, "error": "缺少 audio 字段"}, status_code=400)

    f = files[file_index]
    try:
        audio_name = await run_in_threadpool(
            audio_store.save_audio, meeting_id, file_index, up.file,
            up.filename or f.get("name", ""),
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=413)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"音频保存失败：{e}"}, status_code=500)

    f["audio_name"] = audio_name
    f["has_audio"] = True
    now = _now_iso()
    cols = extract_history_columns(payload)
    try:
        await run_in_threadpool(db.update_payload, meeting_id, payload, cols, now)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"更新失败：{e}"}, status_code=500)
    return JSONResponse({"ok": True, "audio_name": audio_name})


@app.delete("/history/{meeting_id}/files/{file_index}")
async def history_delete_file(meeting_id: int, file_index: int):
    """删除某会议中的单个文件卡片（录音段/上传音频）。返回 { ok }。

    **不重排数组**：file_index 同时是数组下标、路由参数、SSE 帧键、音频文件名前缀
    与在途任务身份（见 CLAUDE.md），重排会让后续每个下标与磁盘上 `{index}_` 开头的
    文件名全部错位，并让在途转写结果写回错误的槽位。删除改为**原地留墓碑**：槽位
    保留、内容清空（services.tombstone_file），索引空间保持稳定。

    因此有两种情况必须拒绝，否则会造成真实的数据错乱：

    - **该槽位正在录音**（`_recorder.active_file_index`）：ffmpeg 正往它的 `.raw`
      里写，此刻把槽位抹成墓碑会让停止时的收尾回调把一个已删条目写回去，且音频
      文件已随删除消失。
    - **该槽位有在途转写任务**（queued/processing）：任务完成时的
      `_persist_transcribe_result` 按下标写回，会把结果写进墓碑。两种情形都返回
      409，让用户等它跑完再删。
    """
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    payload = item.get("payload") or {}
    files = payload.get("files") or []
    if file_index < 0 or file_index >= len(files):
        return JSONResponse({"ok": False, "error": "文件索引越界"}, status_code=404)

    entry = files[file_index]
    if is_deleted_file(entry):
        return JSONResponse({"ok": True})  # 幂等：已删过，直接当成功
    if _recorder.active_file_index(meeting_id) == file_index:
        return JSONResponse({"ok": False, "error": "该段正在录音，请先停止录音"}, status_code=409)
    if file_index in _jobs.active_file_indices(meeting_id):
        return JSONResponse({"ok": False, "error": "该段正在转写中，请等待完成"}, status_code=409)

    # 音频名要在清空之前取：墓碑会把 audio_name 抹掉，之后就没法定位磁盘上的文件了。
    audio_name = str(entry.get("audio_name", "") or "")

    files[file_index] = tombstone_file(entry)
    payload["files"] = files
    payload["status"] = derive_meeting_status(files)
    try:
        await run_in_threadpool(db.update_payload, meeting_id, payload,
                                extract_history_columns(payload), _now_iso())
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"更新失败：{e}"}, status_code=500)

    # 落库成功后再删磁盘音频：反过来的话，删文件成功而落库失败会留下「条目还在、
    # 音频没了」的破损记录。此处失败只留孤儿文件（该目录随会议删除一起清理），
    # 不影响用户看到的结果。
    if audio_name:
        await run_in_threadpool(audio_store.delete_file_audio, meeting_id, audio_name)

    # 广播：文件级墓碑帧让其它标签页把卡片抹掉，会议级帧刷新左栏角标
    _jobs.broadcast(meeting_id, {"type": "file", "file_index": file_index,
                                 "status": FILE_DELETED})
    _jobs.broadcast(meeting_id, {"type": "meeting", "status": payload["status"]})
    return JSONResponse({"ok": True, "status": payload["status"]})


async def _apply_history_update(meeting_id: int, raw_payload) -> JSONResponse:
    """PUT 与 beacon-POST 共用的更新逻辑：校验 payload → 更新行。

    录音护栏：前端 autosave 整份 PUT 它内存里的 files 数组，若那份内存态早于录音
    开始，这次 PUT 会把服务端刚建好的录音条目整个抹掉——这是服务端唯一会被前端整体
    覆盖的写入口，也是录音期间唯一真实的写竞争（录音期间服务端不写段落）。护栏刻意
    做窄：仅在「该会议确实在录」且「PUT 的 files 恰好指向同一索引」时，用服务端已知
    的条目盖回该槽位，其余字段一律照旧（见 services.preserve_recording_file）。
    """
    try:
        norm = validate_history_payload(raw_payload, max_bytes=HISTORY_PAYLOAD_MAX_BYTES)
    except ValueError as e:
        code = 413 if "过大" in str(e) else 400
        return JSONResponse({"ok": False, "error": str(e)}, status_code=code)

    # 录音中的条目：以服务端登记表为准，不信任前端可能过期的内存态
    rec_index = _recorder.active_file_index(meeting_id)
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    cur_files = ((item or {}).get("payload") or {}).get("files") or []
    if rec_index is not None and 0 <= rec_index < len(cur_files):
        norm = preserve_recording_file(norm, rec_index, cur_files[rec_index])
    # 已删除的槽位：墓碑不可逆，整份 PUT 不能把它写回来。与上面同源的竞争——
    # 删除请求与 autosave 同时在路上时，基于删除前内存态的那次 PUT 会让卡片
    # 「删了又自己长出来」。见 services.preserve_deleted_files。
    norm = preserve_deleted_files(norm, cur_files)

    now = _now_iso()
    cols = extract_history_columns(norm)
    try:
        ok = await run_in_threadpool(db.update_payload, meeting_id, norm, cols, now)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"更新失败：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    return JSONResponse({"ok": True, "updated_at": now})


@app.put("/history/{meeting_id}")
async def history_update(meeting_id: int, payload: dict = Body(...)):
    """更新既有会议（JSON,不含音频）：自动保存“更新同一条”路径。

    请求体 { payload }；payload 保留既有 audio_name（前端从恢复/创建结果带回）。
    返回 { ok, updated_at }；id 不存在 404。
    """
    raw = payload.get("payload") if isinstance(payload, dict) and "payload" in payload else payload
    return await _apply_history_update(meeting_id, raw)


@app.post("/history/{meeting_id}")
async def history_update_beacon(meeting_id: int, request: Request):
    """卸载期 navigator.sendBeacon 的更新入口（beacon 只能发 POST）。

    body 为 { payload } 的 JSON；等价于 PUT，但容忍 beacon 无法自定义方法/头的限制。
    仅在 ?beacon=1 时启用，避免与其它 POST 语义混淆。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body 不是合法 JSON"}, status_code=400)
    raw = body.get("payload") if isinstance(body, dict) and "payload" in body else body
    return await _apply_history_update(meeting_id, raw)


@app.get("/history")
async def history_list(page: int = 1, page_size: int = 20, q: str = ""):
    """分页列出历史会议（不含 payload），按日期倒序。返回 { ok, items, total, page, page_size, groups }。

    groups 为后端按天分组的便利结构（前端也可自行用 items 分组）。
    """
    try:
        result = await run_in_threadpool(db.list_meetings, page, page_size, q)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"查询失败：{e}"}, status_code=500)
    result["ok"] = True
    result["groups"] = group_by_date(result["items"])
    return JSONResponse(result)


@app.get("/history/{meeting_id}")
async def history_get(meeting_id: int):
    """取单条完整记录（含 payload）。返回 { ok, item }；不存在 404。"""
    try:
        item = await run_in_threadpool(db.get_meeting, meeting_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"查询失败：{e}"}, status_code=500)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    return JSONResponse({"ok": True, "item": item})


@app.get("/history/{meeting_id}/audio/{file_index}")
async def history_audio(meeting_id: int, file_index: int, request: Request):
    """回放某会议某文件的原始音频，支持 HTTP Range（<audio> 拖动进度条依赖 206）。"""
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    files = (item.get("payload") or {}).get("files") or []
    if file_index < 0 or file_index >= len(files):
        return JSONResponse({"ok": False, "error": "文件索引越界"}, status_code=404)
    audio_name = files[file_index].get("audio_name") or ""
    path = await run_in_threadpool(audio_store.audio_path, meeting_id, audio_name)
    if not path:
        return JSONResponse({"ok": False, "error": "音频不存在"}, status_code=404)
    ext = os.path.splitext(audio_name)[1].lower()
    media_type = _AUDIO_MIME.get(ext, "application/octet-stream")
    return _audio_response(path, media_type, request.headers.get("range", ""))


# ---------- B2：服务端后台转写 + SSE 进度 ----------
async def _run_asr_from_disk(meeting_id: int, file_index: int, hotword: str) -> dict:
    """后台任务的 runner：从磁盘读该会议某文件的音频 → 抢推理锁跑 ASR → 返回
    {segments, elapsed}。音频已在提交前落盘，此处只读不写。"""
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        raise RuntimeError("记录不存在")
    files = (item.get("payload") or {}).get("files") or []
    if file_index < 0 or file_index >= len(files):
        raise RuntimeError("文件索引越界")
    audio_name = files[file_index].get("audio_name") or ""
    path = await run_in_threadpool(audio_store.audio_path, meeting_id, audio_name)
    if not path:
        raise RuntimeError("音频不存在，无法转写")

    start_time = time.time()
    async with _get_infer_lock():
        result = await run_in_threadpool(_run_asr, path, hotword)
        sentence_info = result[0].get("sentence_info", [])
        speaker_matches = await run_in_threadpool(
            _compute_speaker_matches, path, sentence_info
        )
    elapsed = time.time() - start_time

    segments = []
    for seg in sentence_info:
        segments.append({
            "start": round(seg["start"] / 1000, 1),
            "end": round(seg["end"] / 1000, 1),
            "speaker": seg.get("spk", "?"),
            "text": rich_transcription_postprocess(seg["text"]),
        })
    return {"segments": segments, "elapsed": round(elapsed, 1),
            "speaker_matches": speaker_matches}


async def _persist_transcribe_result(meeting_id: int, file_index: int, result: dict) -> None:
    """后台任务的 on_result：把转写结果写回 DB payload 的对应文件，并重算会议级
    status。result = {status, segments?, elapsed?, error?}。写回后向该会议 SSE
    订阅者广播一帧会议级状态（文件级终态帧由管理器另发）。"""
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return  # 会议已被删除：丢弃结果
    payload = item.get("payload") or {}
    files = payload.get("files") or []
    if file_index < 0 or file_index >= len(files):
        return
    f = files[file_index]
    # 槽位已被删除（墓碑）：结果无处可写。正常流程下删除路由会对在途任务返回 409，
    # 但「任务刚提交 → 用户删卡 → 任务才结束」这一窗口仍存在，此处兜底丢弃。
    if is_deleted_file(f):
        return
    status = result.get("status")
    if status == transcribe_jobs.DONE:
        f["status"] = "done"
        f["segments"] = result.get("segments") or []
        f["elapsed"] = result.get("elapsed")
        f.pop("error", None)
    else:
        f["status"] = "error"
        f["error"] = result.get("error") or "转写失败"
    payload["status"] = derive_meeting_status(files)
    cols = extract_history_columns(payload)
    await run_in_threadpool(db.update_payload, meeting_id, payload, cols, _now_iso())
    # 声纹自动匹配结果不落库（是一次性提示，且会随声纹库变化而过时）——
    # 完成时单独广播一帧，前端据此静默填充 speakerMap（仅填用户未手动改过的簇）。
    matches = result.get("speaker_matches") or {}
    if status == transcribe_jobs.DONE and matches:
        _jobs.broadcast(meeting_id, {
            "type": "speaker_matches",
            "file_index": file_index,
            "matches": matches,
        })
    # 会议级状态帧（前端据此刷新列表角标 draft/transcribing/done）
    _jobs.broadcast(meeting_id, {"type": "meeting", "status": payload["status"]})


@app.post("/history/{meeting_id}/transcribe/{file_index}")
async def history_transcribe(meeting_id: int, file_index: int, hotword: str = Body("", embed=True)):
    """提交某会议某文件的后台转写任务（B2）。音频须已落盘（先走 POST /history 或
    补传接口）。立即返回 { ok }，实际进度经 GET /history/{id}/events(SSE) 推送。

    去重：该文件已有排队/进行中的任务时返回 409。校验音频存在后再提交，
    避免提交一个注定失败的任务。
    """
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    files = (item.get("payload") or {}).get("files") or []
    if file_index < 0 or file_index >= len(files):
        return JSONResponse({"ok": False, "error": "文件索引越界"}, status_code=404)
    # 已删除的槽位没有音频可转写（墓碑的 audio_name 是空的），直接拒绝，
    # 免得落进下面「音频尚未上传」那句会产生歧义的提示。
    if is_deleted_file(files[file_index]):
        return JSONResponse({"ok": False, "error": "该文件已删除"}, status_code=404)
    audio_name = files[file_index].get("audio_name") or ""
    path = await run_in_threadpool(audio_store.audio_path, meeting_id, audio_name)
    if not path:
        return JSONResponse({"ok": False, "error": "音频尚未上传，无法转写"}, status_code=400)

    hw = hotword if isinstance(hotword, str) else ""

    async def runner():
        return await _run_asr_from_disk(meeting_id, file_index, hw)

    accepted = _jobs.submit(meeting_id, file_index, runner, _persist_transcribe_result)
    if not accepted:
        return JSONResponse({"ok": False, "error": "该文件正在转写中"}, status_code=409)
    return JSONResponse({"ok": True})


@app.get("/history/{meeting_id}/events")
async def history_events(meeting_id: int, request: Request):
    """某会议的转写进度事件流（SSE）。连接即先发一帧全量快照（各文件当前 status，
    合并进程内活动态），随后推送文件级/会议级增量事件。客户端断开自动清理订阅。"""
    item = await run_in_threadpool(db.get_meeting, meeting_id)
    if item is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    payload = item.get("payload") or {}

    queue = _jobs.subscribe(meeting_id)

    async def event_stream():
        try:
            # 连接即发全量快照（进程内活动态覆盖落库态）。recordings 单独传入：
            # 录音不属于转写登记表（不占推理锁、不进 _jobs），但同样是「活动态」，
            # 前端据此渲染「录制中」并从真实起点续计时（刷新不归零）。
            snap = transcribe_jobs.snapshot_event(
                payload,
                _jobs.active_file_indices(meeting_id),
                _jobs.active_started_at(meeting_id),
                _recorder.active_started_at(meeting_id),
            )
            yield transcribe_jobs.sse_frame(snap)
            while True:
                # 有事件即发；15s 无事件发一次心跳注释，兼顾断连探测与代理保活
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield transcribe_jobs.sse_frame(ev)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keep-alive\n\n"
        finally:
            _jobs.unsubscribe(meeting_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.patch("/history/{meeting_id}")
async def history_rename(meeting_id: int, payload: dict = Body(...)):
    """重命名：仅改冗余列 title + updated_at（不动 payload）。返回 { ok }。"""
    title = str(payload.get("title", "") or "").strip()
    if not title:
        return JSONResponse({"ok": False, "error": "标题不能为空"}, status_code=400)
    if len(title) > 200:
        return JSONResponse({"ok": False, "error": "标题过长"}, status_code=400)
    now = _now_iso()
    try:
        ok = await run_in_threadpool(db.update_title, meeting_id, title, now)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"重命名失败：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    return JSONResponse({"ok": True, "updated_at": now})


@app.delete("/history/{meeting_id}")
async def history_delete(meeting_id: int):
    """删除会议：先删 DB 行（保证列表立即消失、逻辑一致），再删磁盘音频目录。

    先行后文件：即便删目录失败也只留孤儿音频（sweep_orphan_audio 兜底），
    不会出现“行在但音频没了”的破损记录。返回 { ok }；不存在 404。
    """
    # 先停录音并收尾（补 WAV 头 → 落库 → 会提交一次转写），再取消在途任务、删行。
    # 顺序要紧：反过来的话，删行后录音收尾的落库回调写回一条已不存在的记录，
    # 音频目录紧接着被删，最后一段录音就白丢了。
    try:
        await _recorder.stop_meeting(meeting_id)
    except Exception:  # 录音收尾失败不应阻止删会议
        pass
    # 再取消该会议在途的后台转写任务，避免任务在删行后仍写回一条已不存在的记录
    await _jobs.cancel_meeting(meeting_id)
    try:
        ok = await run_in_threadpool(db.delete_meeting, meeting_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"删除失败：{e}"}, status_code=500)
    if not ok:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)
    # 删行成功后清理音频目录（失败不影响响应，靠孤儿清理兜底）
    try:
        await run_in_threadpool(audio_store.delete_meeting_audio, meeting_id)
    except Exception:
        pass
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("EDGEMINUTE_HOST", "0.0.0.0")
    port = env_int("EDGEMINUTE_PORT", 80)
    uvicorn.run(app, host=host, port=port)
