"""音频文件的磁盘存储层（副作用封装）。

一场会议的音频按 `{AUDIO_DIR}/{meeting_id}/{file_index}_{safe_name}` 存放，
按会议 id 建子目录，便于删除会议时整目录清理。

与 db.py（关系库）分离：此模块只管文件系统，db.py 只管 SQLite。
所有路径都被约束在 AUDIO_DIR 之内，防止越界写入/读取（路径穿越防护）。
"""
from __future__ import annotations

import os
import re
import shutil

from services import env_int

# 音频存储根目录（可用环境变量覆盖，与 app.py 的 env 风格一致）
AUDIO_DIR = os.environ.get(
    "AUDIO_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "audio"),
)
# 单个音频文件大小上限（字节），默认 500MB
AUDIO_MAX_BYTES = env_int("AUDIO_MAX_BYTES", 500 * 1024 * 1024)

# 录音大小上限（字节），默认 10GB。与 AUDIO_MAX_BYTES 分开：那条只在 save_audio
# 里边写边计数，而录音是 ffmpeg 直写目标文件，服务端不经手字节流，必须另立一道
# 闸门（由 recorder.py 的看护协程轮询文件大小来执行），否则长会议会无声超限。
# 10GB 对应约 15.5 小时——48k 立体声 16bit 是 192KB/s，比 16k 单声道的 32KB/s 快 6 倍。
RECORD_MAX_BYTES = env_int("RECORD_MAX_BYTES", 10 * 1024 * 1024 * 1024)

# 文件名清洗：仅保留字母数字、点、下划线、连字符；其余（含路径分隔符、空格）替换为下划线
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
# 常见音频扩展名白名单；未知扩展回退为 .bin
_AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma", ".webm", ".opus"}


def init_store() -> None:
    """确保音频根目录存在（服务启动时调用）。"""
    os.makedirs(AUDIO_DIR, exist_ok=True)


def _safe_ext(filename: str) -> str:
    """从原始文件名取安全扩展名（小写、白名单校验）。"""
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in _AUDIO_EXT else ".bin"


def _safe_name(filename: str, file_index: int) -> str:
    """把原始文件名清洗为服务端安全存储名：`{index}_{清洗后主名}{扩展}`。

    主名去掉路径与危险字符并限长，避免超长/穿越文件名。
    """
    base = os.path.basename(filename or "")
    stem = os.path.splitext(base)[0]
    stem = _UNSAFE_NAME.sub("_", stem).strip("._") or "audio"
    stem = stem[:80]  # 限长，防止超长文件名
    return f"{file_index}_{stem}{_safe_ext(filename)}"


def _meeting_dir(meeting_id: int) -> str:
    """返回某会议音频子目录的绝对路径（不创建）。"""
    return os.path.join(AUDIO_DIR, str(int(meeting_id)))


def save_audio(meeting_id: int, file_index: int, src_fileobj, orig_name: str) -> str:
    """把上传的音频流写入 `{AUDIO_DIR}/{meeting_id}/{index}_{name}`，返回存储文件名。

    - src_fileobj: 具有 .read()/可被 shutil.copyfileobj 读取的二进制流（UploadFile.file）。
    - 超过 AUDIO_MAX_BYTES 抛 ValueError（边写边计数，避免整份读入内存）。
    - 返回值为**相对该会议目录的文件名**（audio_name），存入 payload。
    失败时清理已写入的半成品文件。
    """
    dest_dir = _meeting_dir(meeting_id)
    os.makedirs(dest_dir, exist_ok=True)
    audio_name = _safe_name(orig_name, file_index)
    dest_path = os.path.join(dest_dir, audio_name)

    written = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = src_fileobj.read(1024 * 1024)  # 1MB 分块
                if not chunk:
                    break
                written += len(chunk)
                if written > AUDIO_MAX_BYTES:
                    raise ValueError(f"音频文件过大（上限 {AUDIO_MAX_BYTES // (1024*1024)}MB）")
                out.write(chunk)
    except Exception:
        # 清理半成品，避免留下损坏文件
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        raise
    return audio_name


def audio_path(meeting_id: int, audio_name: str) -> str | None:
    """返回某会议某音频的绝对路径；文件不存在或越界返回 None。

    audio_name 必须是纯文件名（无路径分隔符），并解析后确认仍在会议目录内，
    防止 `..`/绝对路径穿越读取任意文件。
    """
    if not audio_name or os.path.basename(audio_name) != audio_name:
        return None
    dest_dir = _meeting_dir(meeting_id)
    path = os.path.normpath(os.path.join(dest_dir, audio_name))
    # 二次确认解析后的路径仍位于该会议目录下
    if os.path.commonpath([os.path.abspath(dest_dir), os.path.abspath(path)]) != os.path.abspath(dest_dir):
        return None
    return path if os.path.isfile(path) else None


def recording_target(meeting_id: int, raw_name: str) -> tuple[str, str]:
    """返回录音的目标绝对路径 `(raw_path, wav_path)`，并确保会议目录已创建。

    这是**唯一**允许先于文件存在而返回路径的入口：录音必须把目标路径交给 ffmpeg
    直接写（save_audio 以 "wb" 打开、只覆盖不追加，且服务端不经手字节流，用不了）。
    正因如此，防护必须在**生成路径时**做，而不是像 audio_path 那样在读取时做。

    raw_name 由 services.recording_raw_name 生成，理论上是受控的 `{index}_mic.raw`；
    这里照搬 audio_path 的同一套校验（纯文件名 + commonpath 复检），使调用方即便
    传入被污染的串也不会写到会议目录之外。
    """
    if not raw_name or os.path.basename(raw_name) != raw_name:
        raise ValueError(f"非法录音文件名：{raw_name!r}")
    dest_dir = _meeting_dir(meeting_id)
    raw_path = os.path.normpath(os.path.join(dest_dir, raw_name))
    if os.path.commonpath([os.path.abspath(dest_dir), os.path.abspath(raw_path)]) != os.path.abspath(dest_dir):
        raise ValueError(f"录音路径越界：{raw_name!r}")
    wav_path = raw_path[:-len(".raw")] + ".wav" if raw_path.endswith(".raw") else raw_path + ".wav"
    os.makedirs(dest_dir, exist_ok=True)
    return raw_path, wav_path


def delete_meeting_audio(meeting_id: int) -> None:
    """删除某会议的整个音频子目录（幂等：目录不存在也不报错）。"""
    dest_dir = _meeting_dir(meeting_id)
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)


def delete_file_audio(meeting_id: int, audio_name: str) -> bool:
    """删除某会议下某个音频文件（含可能同名的采集裸流与错误日志）。

    用于删除单个文件卡片——删除槽位时把它占的音频一起清掉，否则会成为无人引用的
    孤儿（sweep_orphan_audio 只按会议目录扫，扫不到目录内的孤儿文件）。
    与 delete_meeting_audio 一样幂等：文件不存在返回 False 而非报错。

    顺带清理 `{stem}.raw` / `{stem}.raw.err`：录音的成品 WAV 与采集裸流同名不同
    后缀，只删 WAV 会留下裸流；而裸流补头逻辑（启动扫描）只认 payload 里 status=
    recording 的槽位，墓碑槽位不会被它捡回去，留下的裸流就是纯占位。
    """
    if not audio_name:
        return False
    # 复用 audio_path 的同一套穿越防护：先算路径，再确认它确实落在会议目录内。
    # audio_path 要求文件存在（不存在返回 None），删除也应当如此——已不存在即无
    # 事可做，返回 False。
    path = audio_path(meeting_id, audio_name)
    if not path:
        return False
    removed = False
    stem = os.path.splitext(path)[0]
    targets = [path]
    if stem != path:
        # 录音成品：连带采集裸流与其 stderr 日志（同名不同后缀）
        targets += [stem + ".raw", stem + ".raw.err"]
    for p in targets:
        try:
            os.remove(p)
            removed = True
        except OSError:
            pass
    return removed


def sweep_orphan_audio(valid_ids: set[int]) -> list[str]:
    """清理孤儿音频目录：AUDIO_DIR 下 id 不在 valid_ids 的子目录一律删除。

    valid_ids 应为 meetings 表当前全部 id。返回被清理的目录名列表（供日志）。
    作为「删行成功、删目录失败」的兜底，通常在服务启动时调用一次。
    """
    removed: list[str] = []
    if not os.path.isdir(AUDIO_DIR):
        return removed
    for name in os.listdir(AUDIO_DIR):
        full = os.path.join(AUDIO_DIR, name)
        if not os.path.isdir(full):
            continue
        # 目录名应为纯数字会议 id；非数字或不在有效集合 → 孤儿
        if not name.isdigit() or int(name) not in valid_ids:
            shutil.rmtree(full, ignore_errors=True)
            removed.append(name)
    return removed
