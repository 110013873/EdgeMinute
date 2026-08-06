"""服务端后台转写任务编排（B2：真后台转写）。

职责边界：
- 本模块只管「任务生命周期 + 进度事件广播」，不含 ASR 推理本身，也不直连 DB
  写库——推理函数与持久化回调由 app.py 以依赖注入方式传入，保持本模块可脱离
  GPU/模型单元测试（与 services.py 同样的“无模型依赖”约定）。
- 进程内登记表 registry：meeting_id -> {file_index -> JobState}。单进程单模型，
  不引入任务队列/表；服务重启后残留的 transcribing 由 app.py 启动时扫描复位。
- 每个会议一个事件总线（asyncio.Queue 集合），供 SSE 端点订阅；订阅即先收一帧
  全量快照，随后收增量事件。SSE 断开不影响任务（任务不持有订阅者引用）。

文件级状态机：pending → queued → processing → done / error
会议级状态由 services.derive_meeting_status 依据文件态汇总（此处不重复实现）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

# 文件级状态常量（与 services 文件级语义一致；此处只用到活动态与终态）
QUEUED = "queued"
PROCESSING = "processing"
DONE = "done"
ERROR = "error"


class JobState:
    """单个「会议某文件」的转写任务运行态（进程内，不持久化）。"""

    __slots__ = ("meeting_id", "file_index", "status", "task", "error")

    def __init__(self, meeting_id: int, file_index: int):
        self.meeting_id = meeting_id
        self.file_index = file_index
        self.status = QUEUED
        self.task: asyncio.Task | None = None
        self.error: str = ""


class TranscribeManager:
    """转写任务管理器：提交任务、维护登记表、向会议事件总线广播进度。"""

    def __init__(self) -> None:
        # meeting_id -> file_index -> JobState
        self._jobs: dict[int, dict[int, JobState]] = {}
        # meeting_id -> set[asyncio.Queue]（该会议的 SSE 订阅者）
        self._subscribers: dict[int, set[asyncio.Queue]] = {}

    # ---------- 订阅（SSE 端点使用）----------
    def subscribe(self, meeting_id: int) -> asyncio.Queue:
        """订阅某会议的进度事件，返回一个事件队列。调用方负责最终 unsubscribe。"""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(meeting_id, set()).add(q)
        return q

    def unsubscribe(self, meeting_id: int, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(meeting_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(meeting_id, None)

    def broadcast(self, meeting_id: int, event: dict) -> None:
        """把事件投递给该会议所有订阅者（非阻塞）。无订阅者则静默丢弃。

        供 app.py 在 on_result 内广播会议级状态帧；内部文件级事件也走此方法。
        """
        for q in list(self._subscribers.get(meeting_id, ())):
            q.put_nowait(event)

    # 内部旧名保留为别名，避免大面积改动内部调用点
    _broadcast = broadcast

    # ---------- 查询 ----------
    def has_active(self, meeting_id: int) -> bool:
        """该会议是否有排队/进行中的文件任务。"""
        jobs = self._jobs.get(meeting_id, {})
        return any(j.status in (QUEUED, PROCESSING) for j in jobs.values())

    def active_file_indices(self, meeting_id: int) -> list[int]:
        jobs = self._jobs.get(meeting_id, {})
        return sorted(i for i, j in jobs.items() if j.status in (QUEUED, PROCESSING))

    # ---------- 提交任务 ----------
    def submit(
        self,
        meeting_id: int,
        file_index: int,
        runner: Callable[[], Awaitable[dict]],
        on_result: Callable[[int, int, dict], Awaitable[None]],
    ) -> bool:
        """提交一个后台转写任务。

        - runner：无参协程，实际执行 ASR（含读磁盘音频 + 抢推理锁），返回
          {segments, elapsed} 或抛异常。
        - on_result：任务完成/失败后的持久化回调，签名 (meeting_id, file_index, result)，
          result 形如 {status, segments?, elapsed?, error?}；由 app.py 负责写回 DB payload。
        返回 False 表示该文件已有活动任务（去重，避免并发重复转写同一文件）。
        """
        per = self._jobs.setdefault(meeting_id, {})
        existing = per.get(file_index)
        if existing and existing.status in (QUEUED, PROCESSING):
            return False

        state = JobState(meeting_id, file_index)
        per[file_index] = state
        # 入队即广播 queued（前端立即看到“排队中”）
        self._broadcast(meeting_id, self._file_event(state))
        state.task = asyncio.create_task(
            self._run(state, runner, on_result)
        )
        return True

    async def _run(
        self,
        state: JobState,
        runner: Callable[[], Awaitable[dict]],
        on_result: Callable[[int, int, dict], Awaitable[None]],
    ) -> None:
        mid, idx = state.meeting_id, state.file_index
        try:
            state.status = PROCESSING
            self._broadcast(mid, self._file_event(state))
            out = await runner()  # {segments, elapsed, speaker_matches}
            state.status = DONE
            result = {
                "status": DONE,
                "segments": out.get("segments", []),
                "elapsed": out.get("elapsed"),
                # 声纹自动匹配结果需透传给 on_result，否则那一帧永远发不出（历史 bug）
                "speaker_matches": out.get("speaker_matches") or {},
            }
        except asyncio.CancelledError:
            # 任务被显式取消（如会议删除）：不写回，直接清理
            self._cleanup(state)
            raise
        except Exception as e:  # noqa: BLE001 - 转写失败需转为文件级 error 态
            state.status = ERROR
            state.error = str(e)
            result = {"status": ERROR, "error": str(e)}

        # 持久化回调（写 DB payload）——失败不影响状态广播，但要记进事件
        persist_error = ""
        try:
            await on_result(mid, idx, result)
        except Exception as e:  # noqa: BLE001
            persist_error = str(e)

        ev = self._file_event(state)
        if persist_error:
            ev["persist_error"] = persist_error
        # 携带会议级最新状态由 app.py 在 on_result 内计算并另发；此处发文件终态帧
        self._broadcast(mid, ev)
        self._cleanup(state)

    def _cleanup(self, state: JobState) -> None:
        """任务终态后从登记表移除该文件条目；会议无剩余任务时移除会议条目。"""
        per = self._jobs.get(state.meeting_id)
        if not per:
            return
        # 仅当登记表里仍是这个 state 才移除（防止被新任务覆盖后误删）
        if per.get(state.file_index) is state:
            per.pop(state.file_index, None)
        if not per:
            self._jobs.pop(state.meeting_id, None)

    async def cancel_meeting(self, meeting_id: int) -> None:
        """取消某会议的全部在途任务（会议删除时调用）。"""
        per = self._jobs.get(meeting_id, {})
        tasks = [j.task for j in per.values() if j.task and not j.task.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._jobs.pop(meeting_id, None)

    # ---------- 事件构造 ----------
    @staticmethod
    def _file_event(state: JobState) -> dict:
        ev = {
            "type": "file",
            "file_index": state.file_index,
            "status": state.status,
        }
        if state.status == ERROR and state.error:
            ev["error"] = state.error
        return ev


def sse_frame(event: dict) -> str:
    """把事件对象序列化为一帧 SSE 文本（data: {...}\\n\\n）。"""
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def snapshot_event(payload: dict, active_indices: list[int]) -> dict:
    """构造「连接即发」的全量快照事件：各文件当前 status（合并进程内活动态）。

    payload：会议当前 DB payload；active_indices：登记表中仍在跑的文件下标。
    进程内活动态优先于 payload 落库态（落库可能滞后于内存进度）。
    """
    files = (payload or {}).get("files") or []
    active = set(active_indices or [])
    out_files = []
    for i, f in enumerate(files):
        st = PROCESSING if i in active else str(f.get("status", "") or "")
        out_files.append({
            "file_index": i,
            "status": st or ("done" if (f.get("segments") or []) else "pending"),
            "name": f.get("name", ""),
        })
    return {
        "type": "snapshot",
        "meeting_status": (payload or {}).get("status", ""),
        "files": out_files,
    }
