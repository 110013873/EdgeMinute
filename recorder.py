"""服务端麦克风录音编排（与 transcribe_jobs.py 同构）。

职责边界（与 transcribe_jobs.py 保持一致的分层）：
- 本模块只管「采集子进程的生命周期 + 状态广播」，**不 import db、不 import app**。
  条目落库、转写提交一律由 app.py 以依赖注入方式回调进来，使本模块可脱离
  SQLite / GPU / ffmpeg 单元测试（fake spawn 即可）。
- 进程内登记表：meeting_id -> RecorderState，一场会议同时最多一路录音。单进程
  用不完的复杂度不引入（无任务队列、无持久化运行态）。

采集写的是**无头裸流**（services.recording_raw_name），停止时按实际文件大小补
WAV 头（services.wrap_raw_to_wav）。这样进程被 SIGKILL / 断电时，已落盘的任意长度
前缀都是合法音频，服务重启后仍能收尾——这是本模块最核心的健壮性来源。

状态机：recording → (停止/异常) → pending（可转写）/ error。
裸流不删除：补头后留作诊断，且幂等重跑随时可用。
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from typing import Any, Awaitable, Callable

import services

# 文件级状态（此处只用到录音相关几个，与 services 的语义一致）
RECORDING = "recording"
PENDING = "pending"
ERROR = "error"

# 停止录音时，先给 ffmpeg 一段体面收尾的时间（写缓冲刷盘），超时才强杀。
STOP_GRACE_SECONDS = 5.0
# 看护协程轮询间隔：兼顾掉麦检测的及时性与长会议的开销。
WATCH_INTERVAL_SECONDS = 2.0


async def _to_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """asyncio.to_thread 的 Python 3.8 兼容实现（本项目的部署基线）。

    Jetson JetPack 5.1.2 自带 Python 3.8，而 asyncio.to_thread 是 3.9 才加入的，
    直接调用会在运行期抛 AttributeError。语义等价：把阻塞调用丢进默认线程池，
    不占用事件循环。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


def build_capture_cmd(device: str, raw_path: str) -> list[str]:
    """构造 ffmpeg ALSA 采集命令（纯函数，便于断言与单测）。

    - 按 REC_* 录成 48k/立体声/16bit：**保回放音质优先**，不再迁就 ASR 的 16k。
      转 16k 单声道由 ASR 侧统一做（app.py 的 _ensure_audio_input，对上传文件也一视
      同仁），所以录音与上传走的是同一条转码路，采集端不做任何格式裁剪。
    - -f s16le 输出**裸流**（无容器头），崩溃安全，见模块 docstring。
    - -nostdin：ffmpeg 默认会读 stdin 做交互键，服务端必须关掉，否则抢占终端。
    - -flush_packets 1：**必须**。s16le 裸流默认走 AVIO 的缓冲，ffmpeg 攒够一大块
      （实测约 8 秒）才落盘一次，由此带来两个问题：（1）前端计时器与电平表每 8 秒
      才跳一下；（2）进程被 SIGKILL 时未刷出的那 8 秒音频直接丢失——而崩溃安全正是
      选裸流的全部理由，缓冲把这层保障削掉了。逐包刷盘的开销在 192KB/s 的码率下
      仍然可忽略（它是每包一次 write 系统调用，与码率无关）。
    """
    return [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-f", "alsa", "-i", device,
        "-ac", str(services.REC_CHANNELS),
        "-ar", str(services.REC_SAMPLE_RATE),
        "-flush_packets", "1",
        "-f", "s16le",
        "-y", raw_path,
    ]


class RecorderState:
    """一路录音的运行态（进程内，不持久化）。"""

    __slots__ = ("meeting_id", "file_index", "device", "proc", "raw_path",
                 "wav_path", "started_at", "stopping", "hotword", "watch")

    def __init__(self, meeting_id: int, file_index: int, device: str,
                 raw_path: str, wav_path: str, hotword: str = ""):
        self.meeting_id = meeting_id
        self.file_index = file_index
        self.device = device
        self.proc: subprocess.Popen | None = None
        self.raw_path = raw_path
        self.wav_path = wav_path
        self.started_at = time.time()
        # stopping=True 表示这是我们主动停的；看护协程据此把「进程退出」当作预期。
        self.stopping = False
        self.hotword = hotword
        self.watch: asyncio.Task | None = None


def _spawn(cmd: list[str], err_path: str) -> subprocess.Popen:
    """默认的进程启动器：stderr 重定向到文件。

    不能用 subprocess.PIPE——长会议下管道写满会阻塞 ffmpeg（父进程只在停止时才读）。
    落到文件既避免死锁，又能在异常退出时把 ffmpeg 的最后一句错误读给用户。
    """
    err = open(err_path, "wb")
    try:
        return subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
        )
    finally:
        err.close()


def _human_bytes(n: int) -> str:
    """把字节数写成人类可读的 MB/GB（提示文案用，避免 2GB 被整除成 2048MB）。"""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}GB"
    return f"{n / 1024 ** 2:.0f}MB"


def _read_err_tail(err_path: str, limit: int = 400) -> str:
    """读 ffmpeg 错误文件末尾若干字符（失败返回空串）。"""
    try:
        size = os.path.getsize(err_path)
        with open(err_path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


class RecorderManager:
    """录音管理器：启停采集进程、看护异常退出、收尾并回调落库。"""

    def __init__(
        self,
        emit: Callable[[int, dict], None],
        on_finalize: Callable[..., Awaitable[None]],
        spawn: Callable[[list[str], str], subprocess.Popen] = _spawn,
        max_bytes: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        - emit(meeting_id, event)：把事件投给该会议的 SSE 订阅者（app.py 传
          TranscribeManager.broadcast，复用同一条事件总线）。
        - on_finalize(meeting_id, file_index, **info)：收尾后的持久化回调（协程），
          info 形如 {status, duration, error, raw_path, wav_path, hotword}。
          app.py 负责写 payload 并按 status 决定是否提交转写。回调抛异常不影响
          进程清理与状态广播。
        - spawn / clock：可注入，便于单测（无需 ffmpeg、无需真实时间）。
        - max_bytes：录音专用大小上限（0=不限）；触顶自动停止。
        """
        self._recs: dict[int, RecorderState] = {}
        self._emit = emit
        self._on_finalize = on_finalize
        self._spawn = spawn
        self._max_bytes = max(0, int(max_bytes))
        self._clock = clock

    # ---------- 查询 ----------
    def is_recording(self, meeting_id: int) -> bool:
        rec = self._recs.get(meeting_id)
        return bool(rec and not rec.stopping)

    def active_file_index(self, meeting_id: int) -> int | None:
        rec = self._recs.get(meeting_id)
        return rec.file_index if rec and not rec.stopping else None

    def active_started_at(self, meeting_id: int) -> dict[int, float]:
        """{file_index: 起始墙钟秒}，供 SSE 快照让前端计时器刷新后不归零。"""
        rec = self._recs.get(meeting_id)
        if not rec or rec.stopping:
            return {}
        return {rec.file_index: rec.started_at}

    def recorded_seconds(self, meeting_id: int) -> float:
        """已录时长（秒）。没有在录时为 0。

        取「墙钟已过时间」与「裸流大小换算出的时长」中的**大者**。

        为什么要看墙钟：ffmpeg 是带缓冲写盘的，裸流要隔一段才跳一次（现网实测约
        8 秒一次），只看文件大小会让前端的计时器一顿一顿地跳。而缓冲里的音频并没有
        丢——ffmpeg 正常退出时会一并刷盘——所以「已经录了多久」本来就该按墙钟算，
        文件大小只是它的一个滞后下界。

        这里用的是本进程的 wall clock，与 RecorderState.started_at 同一个时钟，
        不存在服务端 / 浏览器之间的时钟偏差问题（前端只是把这个数照搬显示）。
        """
        rec = self._recs.get(meeting_id)
        if not rec:
            return 0.0
        wall = max(0.0, self._clock() - rec.started_at)
        try:
            return max(wall, services.raw_pcm_seconds(os.path.getsize(rec.raw_path)))
        except OSError:
            return wall

    def level(self, meeting_id: int) -> float:
        """当前电平（0–1，读裸流尾部算 RMS）；无录音时为 0。"""
        rec = self._recs.get(meeting_id)
        return services.pcm_rms_tail(rec.raw_path) if rec else 0.0

    # ---------- 启动 ----------
    def start(self, meeting_id: int, file_index: int, device: str,
              raw_path: str, wav_path: str, hotword: str = "") -> bool:
        """启动一路采集。已有录音在跑则返回 False（调用方转 409）。"""
        if self.is_recording(meeting_id):
            return False
        state = RecorderState(meeting_id, file_index, device, raw_path, wav_path, hotword)
        state.started_at = self._clock()
        try:
            state.proc = self._spawn(
                build_capture_cmd(device, raw_path), raw_path + ".err"
            )
        except Exception:
            # 进程起不来（ffmpeg 缺失 / 设备打不开）：不留半个登记项
            self._recs.pop(meeting_id, None)
            raise
        self._recs[meeting_id] = state
        state.watch = asyncio.create_task(self._watch(state))
        self._emit_file(state)
        return True

    # ---------- 停止 ----------
    async def stop(self, meeting_id: int) -> bool:
        """停止并收尾一路录音；没有在录则返回 False（幂等，不报错）。"""
        state = self._recs.pop(meeting_id, None)
        if state is None:
            return False
        state.stopping = True
        await self._terminate(state)
        await self._finalize(state, status=PENDING)
        return True

    async def stop_meeting(self, meeting_id: int) -> bool:
        """会议删除前的收尾（先停录音、补头，再让上层删记录与音频目录）。"""
        return await self.stop(meeting_id)

    async def shutdown(self) -> None:
        """服务关闭时收尾所有在途录音（尽力而为，逐路独立失败）。"""
        for meeting_id in list(self._recs.keys()):
            try:
                await self.stop(meeting_id)
            except Exception:  # noqa: BLE001 - 关闭路径绝不因单路失败而中断
                pass

    async def _terminate(self, state: RecorderState) -> None:
        """先礼后兵：SIGINT 让 ffmpeg 正常收尾，超时再 SIGKILL。"""
        proc = state.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                # POSIX（Jetson 部署目标）：SIGINT 是 ffmpeg 的体面退出信号。
                proc.send_signal(signal.SIGINT)
            except (ValueError, OSError, AttributeError):
                # Windows 不支持给子进程发 SIGINT；terminate 会直接结束它——
                # 对裸流无害（已写部分始终合法），仅可能丢掉最后的缓冲区。
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        try:
            await _to_thread(proc.wait, STOP_GRACE_SECONDS)
        except Exception:  # noqa: BLE001 - timeout 或 wait 失败
            pass
        if proc.poll() is None:
            try:
                proc.kill()
                await _to_thread(proc.wait, STOP_GRACE_SECONDS)
            except Exception:  # noqa: BLE001
                pass
        # 收尾看护协程。注意排除「当前就是它」的情形：触到大小上限时是看护协程
        # 自己调的 _terminate，无脑 cancel 会把自己在 await 中取消，收尾半途夭折。
        watch = state.watch
        if watch and not watch.done() and watch is not asyncio.current_task():
            watch.cancel()

    async def _finalize(self, state: RecorderState, status: str, error: str = "") -> None:
        """补 WAV 头并按 status 回调落库；任何失败都降级为 error 态而非抛出。"""
        info = {
            "status": status,
            "duration": 0.0,
            "error": error,
            "raw_path": state.raw_path,
            "wav_path": state.wav_path,
            "hotword": state.hotword,
        }
        try:
            data_bytes = await _to_thread(
                services.wrap_raw_to_wav, state.raw_path, state.wav_path
            )
            info["duration"] = services.raw_pcm_seconds(data_bytes)
        except OSError as e:
            # 裸流不存在（例如 ffmpeg 一秒都没写成）：保留用户看得懂的提示
            info["status"] = ERROR
            info["error"] = error or f"录音文件不可用：{e}"
        try:
            await self._on_finalize(state.meeting_id, state.file_index, **info)
        except Exception:  # noqa: BLE001 - 持久化失败不应吞掉进程清理
            pass
        self._emit_file(state, status=info["status"], error=info["error"])

    # ---------- 看护 ----------
    async def _watch(self, state: RecorderState) -> None:
        """轮询采集进程：异常退出或触到大小上限时自动收尾。

        这一路覆盖三种现实：麦克风被拔（进程自己退出）、ffmpeg 打开设备失败
        （启动即退）、以及长会议写到磁盘上限。任何情况下都保留已录音频。
        """
        err_path = state.raw_path + ".err"
        while True:
            try:
                await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            if state.stopping:
                return
            if self._recs.get(state.meeting_id) is not state:
                return  # 已被 stop() 接管
            code = state.proc.poll() if state.proc else None
            if code is not None:
                detail = _read_err_tail(err_path)
                msg = f"录音进程意外退出（code={code}）"
                if detail:
                    msg += f"：{detail}"
                self._recs.pop(state.meeting_id, None)
                state.stopping = True
                await self._finalize(state, status=ERROR, error=msg)
                return
            if self._max_bytes:
                try:
                    if os.path.getsize(state.raw_path) >= self._max_bytes:
                        self._recs.pop(state.meeting_id, None)
                        state.stopping = True
                        await self._terminate(state)
                        await self._finalize(
                            state, status=PENDING,
                            error=f"已达录音大小上限（{_human_bytes(self._max_bytes)}），自动停止",
                        )
                        return
                except OSError:
                    pass

    # ---------- 事件构造 ----------
    def _emit_file(self, state: RecorderState, status: str = RECORDING, error: str = "") -> None:
        """广播文件级状态帧（与 transcribe_jobs 的 file 帧同构，前端同一分支处理）。"""
        ev = {
            "type": "file",
            "file_index": state.file_index,
            "status": status,
            "started_at": state.started_at,
        }
        if error:
            ev["error"] = error
        try:
            self._emit(state.meeting_id, ev)
        except Exception:  # noqa: BLE001 - 广播失败不得影响录音本身
            pass
