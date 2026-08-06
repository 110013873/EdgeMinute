"""声纹（SV）向量提取的薄封装层——把“调 CAM++ SV 模型 + soundfile 读音频”这类
带副作用/重依赖的 IO 隔离在此，使 app.py 与测试都通过 set_model 注入模型：

- app.py 启动时若 SV_ENABLED 且模型加载成功，调用 set_model(sv_model) 注入。
- 测试用 set_model 注入一个假的模型（返回定长向量），无需 GPU/soundfile。

对外两个入口：
- embed_file(path)：对一个音频**文件**提向量（登记场景，模型内部自行解码/重采样）。
- embed_array(samples, sr)：对一段**numpy 波形**提向量（转写后按说话人切片的场景）。

两者都返回 L2 归一化后的 list[float]（与 voiceprints/services 的向量约定一致）。
提取失败一律抛异常，由调用方决定回退（登记→报错；匹配→跳过该簇）。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# 由 app.py 注入的 SV AutoModel（None 表示声纹功能未启用）
_sv_model: Optional[Any] = None


def set_model(model: Any) -> None:
    """注入已加载的 SV 模型（或 None 以禁用）。"""
    global _sv_model
    _sv_model = model


def is_enabled() -> bool:
    return _sv_model is not None


def _l2_list(vec: Any) -> list:
    v = [float(x) for x in vec]
    norm = math.sqrt(sum(x * x for x in v))
    if norm <= 1e-12:
        return [0.0 for _ in v]
    return [x / norm for x in v]


def _extract(res: Any) -> list:
    """从 SV 模型 generate 的返回里取出 spk_embedding，展平成一维 list。"""
    emb = res[0]["spk_embedding"]
    # 可能是 numpy / torch.Tensor / 嵌套 list：统一展平
    if hasattr(emb, "detach"):  # torch.Tensor
        emb = emb.detach().cpu().numpy()
    if hasattr(emb, "reshape"):  # numpy
        emb = emb.reshape(-1).tolist()
    elif isinstance(emb, (list, tuple)) and emb and isinstance(emb[0], (list, tuple)):
        emb = list(emb[0])  # (1, D) 形式的嵌套 list
    return _l2_list(emb)


def embed_file(path: str) -> list:
    """对一个音频文件提声纹向量（登记用）。模型未启用则抛 RuntimeError。"""
    if _sv_model is None:
        raise RuntimeError("声纹功能未启用（SV 模型未加载）")
    return _extract(_sv_model.generate(input=path))


def embed_array(samples: Any, sr: int) -> list:
    """对一段波形（numpy 一维 float 数组，采样率 sr）提声纹向量（匹配切片用）。

    CAM++ SV 期望 16k 单声道；调用方（app.py）负责在切片前把整段音频统一到
    16k 单声道，故此处直接把数组交给模型。
    """
    if _sv_model is None:
        raise RuntimeError("声纹功能未启用（SV 模型未加载）")
    return _extract(_sv_model.generate(input=samples))


# CAM++ zh-cn 16k common 期望的采样率
TARGET_SR = 16000


def load_audio_16k_mono(path: str):
    """用 soundfile 读整段音频 → 单声道 float32 → 线性重采样到 16k。返回 numpy 一维数组。

    soundfile（libsndfile）不支持的格式（如 M4A/AAC）自动回退到 ffmpeg 子进程解码。
    """
    import numpy as np
    import soundfile as sf

    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception:
        # M4A/AAC 等格式：用 ffmpeg 解码为原始 float32 PCM
        audio, sr = _load_via_ffmpeg(path)
    if getattr(audio, "ndim", 1) > 1:  # 多声道 → 取均值转单声道
        audio = audio.mean(axis=1)
    audio = np.asarray(audio, dtype="float32").reshape(-1)
    if sr != TARGET_SR and audio.size > 0:
        # 线性插值重采样到 16k
        n_out = int(round(audio.size * TARGET_SR / sr))
        if n_out > 0:
            x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype("float32")
    return audio


def _load_via_ffmpeg(path: str):
    """通过 ffmpeg 子进程将任意音频解码为 16k 单声道 float32 PCM。

    返回 (numpy_array, 16000)。ffmpeg 已在此处完成重采样和声道混合，
    调用方无需再做处理。
    """
    import subprocess

    import numpy as np

    cmd = [
        "ffmpeg",
        "-nostdin", "-loglevel", "error",
        "-i", path,
        "-f", "f32le",         # 32-bit float little-endian raw PCM
        "-acodec", "pcm_f32le",
        "-ac", "1",            # 单声道
        "-ar", str(TARGET_SR), # 重采样到 16k
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(stderr or f"ffmpeg 退出码 {proc.returncode}")
    raw = proc.stdout
    if not raw:
        raise RuntimeError("ffmpeg 输出为空（音频可能已损坏或无声轨）")
    audio = np.frombuffer(raw, dtype="<f4").copy()
    return audio, TARGET_SR


def slice_concat(audio, spans_sec, sr: int):
    """按若干 (start_sec, end_sec) 区间从整段音频切片并首尾拼接。返回 numpy 一维数组。

    区间越界自动裁剪；空结果返回长度 0 的数组（调用方应跳过）。
    """
    import numpy as np

    total = int(getattr(audio, "size", len(audio)))
    pieces = []
    for start_sec, end_sec in spans_sec or []:
        a = max(0, int(start_sec * sr))
        b = min(total, int(end_sec * sr))
        if b > a:
            pieces.append(audio[a:b])
    if not pieces:
        return np.zeros(0, dtype="float32")
    return np.concatenate(pieces).astype("float32")
