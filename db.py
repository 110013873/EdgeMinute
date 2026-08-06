"""会议历史的 SQLite 持久化层（副作用封装）。

- 单表 meetings：完整会话 JSON 存 payload 列，另抽出冗余列供列表/分组/搜索，
  避免列表页反序列化大 blob。音频不进库（见 audio_store.py），payload 仅记引用。
- 每请求新建连接（SQLite 连接不宜跨线程共享）；WAL + busy_timeout 提升并发容忍度。
- 所有函数为同步实现，在 app.py 中用 run_in_threadpool 包裹，避免阻塞事件循环。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

# SQLite 文件路径（可用环境变量覆盖，与 app.py env 风格一致）
DB_PATH = os.environ.get(
    "HISTORY_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "meetings.db"),
)

# 列表页只查这些冗余列（不含大 payload）
_LIST_COLUMNS = (
    "id, title, meeting_date, duration, segment_count, "
    "speaker_count, snippet, status, created_at, updated_at"
)

# 会议级状态：draft=已建档未转写 / transcribing=有文件在转写 / done=全部完成
DEFAULT_STATUS = "done"


def _connect() -> sqlite3.Connection:
    """新建一个配置好的连接：行工厂为 Row，开启 WAL 与写锁等待。"""
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 读写并发容忍
    conn.execute("PRAGMA busy_timeout=5000")     # 写锁最多等 5s，缓解 database is locked
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建表 + 索引 + 幂等迁移（服务启动时调用）。"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meetings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                title         TEXT    NOT NULL DEFAULT '',
                meeting_date  TEXT    NOT NULL DEFAULT '',
                duration      REAL    NOT NULL DEFAULT 0,
                segment_count INTEGER NOT NULL DEFAULT 0,
                speaker_count INTEGER NOT NULL DEFAULT 0,
                snippet       TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'done',
                payload       TEXT    NOT NULL,
                created_at    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL
            )
            """
        )
        # 幂等迁移：为早于 status 列的旧库补列（SQLite 无 ADD COLUMN IF NOT EXISTS）。
        # 旧记录默认 'done'，保持向后兼容（历史记录都是转写完成态）。
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(meetings)").fetchall()}
        if "status" not in cols:
            conn.execute("ALTER TABLE meetings ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meetings_date "
            "ON meetings(meeting_date DESC, created_at DESC)"
        )


def _row_to_list_item(row: sqlite3.Row) -> dict[str, Any]:
    """把冗余列行转为列表项字典（不含 payload）。"""
    return {
        "id": row["id"],
        "title": row["title"],
        "meeting_date": row["meeting_date"],
        "duration": row["duration"],
        "segment_count": row["segment_count"],
        "speaker_count": row["speaker_count"],
        "snippet": row["snippet"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def insert_meeting(payload: dict, columns: dict, now: str) -> int:
    """插入一条会议记录，返回自增 id。

    payload: 完整会话 JSON（dict）；columns: 由 services.extract_history_columns
    计算的冗余列（title/meeting_date/duration/segment_count/speaker_count/snippet）。
    now: ISO8601 时间字符串（created_at=updated_at）。
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO meetings
                (title, meeting_date, duration, segment_count, speaker_count,
                 snippet, status, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                columns.get("title", ""),
                columns.get("meeting_date", ""),
                columns.get("duration", 0),
                columns.get("segment_count", 0),
                columns.get("speaker_count", 0),
                columns.get("snippet", ""),
                columns.get("status", DEFAULT_STATUS),
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def update_payload(meeting_id: int, payload: dict, columns: dict, now: str) -> bool:
    """更新既有记录的 payload 与冗余列（自动保存的“更新同一条”路径）。

    返回是否命中（False 表示 id 不存在）。updated_at 刷新为 now。
    title 也随 payload 同步（若冗余列给出）；重命名请用 update_title 单独改。
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE meetings SET
                title=?, meeting_date=?, duration=?, segment_count=?,
                speaker_count=?, snippet=?, status=?, payload=?, updated_at=?
            WHERE id=?
            """,
            (
                columns.get("title", ""),
                columns.get("meeting_date", ""),
                columns.get("duration", 0),
                columns.get("segment_count", 0),
                columns.get("speaker_count", 0),
                columns.get("snippet", ""),
                columns.get("status", DEFAULT_STATUS),
                json.dumps(payload, ensure_ascii=False),
                now,
                meeting_id,
            ),
        )
        return cur.rowcount > 0


def update_title(meeting_id: int, title: str, now: str) -> bool:
    """仅重命名：改冗余列 title + updated_at，不动 payload。返回是否命中。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE meetings SET title=?, updated_at=? WHERE id=?",
            (title, now, meeting_id),
        )
        return cur.rowcount > 0


def list_meetings(page: int = 1, page_size: int = 20, q: str = "") -> dict:
    """分页列出会议（按 meeting_date、created_at 倒序），不含 payload。

    q 非空时对 title/snippet 做 LIKE 模糊匹配。返回 {items, total, page, page_size}。
    """
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    offset = (page - 1) * page_size
    where, params = "", []
    if q and q.strip():
        where = "WHERE title LIKE ? OR snippet LIKE ?"
        like = f"%{q.strip()}%"
        params = [like, like]

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM meetings {where}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM meetings {where} "
            "ORDER BY meeting_date DESC, created_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, offset],
        ).fetchall()

    return {
        "items": [_row_to_list_item(r) for r in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


def get_meeting(meeting_id: int) -> dict | None:
    """取单条完整记录（含解析后的 payload）；不存在返回 None。"""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_LIST_COLUMNS}, payload FROM meetings WHERE id=?",
            (meeting_id,),
        ).fetchone()
    if row is None:
        return None
    item = _row_to_list_item(row)
    try:
        item["payload"] = json.loads(row["payload"])
    except (ValueError, TypeError):
        item["payload"] = {}
    return item


def delete_meeting(meeting_id: int) -> bool:
    """删除记录行（音频目录由调用方在删行成功后清理）。返回是否命中。"""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
        return cur.rowcount > 0


def all_meeting_ids() -> set[int]:
    """返回当前全部会议 id，供 audio_store.sweep_orphan_audio 清理孤儿目录。"""
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM meetings").fetchall()
    return {int(r["id"]) for r in rows}


def ids_by_status(status: str) -> list[int]:
    """返回指定会议级 status 的全部 id，供启动时扫描复位残留 transcribing。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id FROM meetings WHERE status=?", (status,)
        ).fetchall()
    return [int(r["id"]) for r in rows]
