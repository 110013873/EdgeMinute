"""声纹库的 SQLite 持久化层（副作用封装）。

- 两表：
  - voiceprint_people：每个登记人物一行，含姓名/单位/职务 + **平均后**的声纹向量
    （由其全部样本求均值再 L2 归一化）+ sample_count（冗余，便于列表展示）。
  - voiceprint_samples：每条登记样本一行（person_id 外键 + 原始声纹向量）。
    保留每条样本向量，使“删除某条样本后重算平均”“一人多样本”成立；
    但**不保留原始音频**（提完向量即丢，见 app.py 登记接口）。
- 向量以 float32 小端字节存 BLOB（numpy tobytes），读回 reshape 成一维 list。
- 每请求新建连接；WAL + busy_timeout（与 db.py 一致）。
- 所有函数为同步实现，在 app.py 中用 run_in_threadpool 包裹，避免阻塞事件循环。
- 人物主键为自增 id（**非姓名**）：允许重名的不同人各自独立登记。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

import numpy as np

from services import env_int

# SQLite 文件路径（可用环境变量覆盖，与 db.py 风格一致）
VOICEPRINT_DB_PATH = os.environ.get(
    "VOICEPRINT_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "voiceprints.db"),
)

# 声纹向量维度（CAM++ zh-cn 16k common = 192）。仅用于读回时 reshape 校验。
EMBEDDING_DIM = env_int("VOICEPRINT_EMBEDDING_DIM", 192)


def _connect() -> sqlite3.Connection:
    """新建一个配置好的连接：行工厂为 Row，开启 WAL 与写锁等待、外键。"""
    conn = sqlite3.connect(VOICEPRINT_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """建表 + 索引（服务启动时调用）。"""
    os.makedirs(os.path.dirname(VOICEPRINT_DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voiceprint_people (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                unit         TEXT    NOT NULL DEFAULT '',
                title        TEXT    NOT NULL DEFAULT '',
                embedding    BLOB    NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voiceprint_samples (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id  INTEGER NOT NULL,
                embedding  BLOB    NOT NULL,
                created_at TEXT    NOT NULL,
                FOREIGN KEY (person_id) REFERENCES voiceprint_people(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vp_samples_person "
            "ON voiceprint_samples(person_id)"
        )


# ---------- 向量 <-> BLOB 编解码 ----------

def _to_blob(vec: Any) -> bytes:
    """把一维数值序列编码为 float32 小端字节。"""
    arr = np.asarray(vec, dtype="<f4").reshape(-1)
    return arr.tobytes()


def _from_blob(blob: bytes) -> list[float]:
    """把 BLOB 解回一维 float list。"""
    arr = np.frombuffer(blob, dtype="<f4")
    return [float(x) for x in arr]


def _l2_normalize_np(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return vec.astype("<f4")
    return (vec / norm).astype("<f4")


def _recompute_average(conn: sqlite3.Connection, person_id: int) -> tuple[bytes, int]:
    """由某人全部样本重算平均声纹（均值后 L2 归一化）。返回 (blob, sample_count)。

    无样本时返回全零向量（EMBEDDING_DIM 维）与 0——调用方通常在删空后删除该人物。
    """
    rows = conn.execute(
        "SELECT embedding FROM voiceprint_samples WHERE person_id=?", (person_id,)
    ).fetchall()
    if not rows:
        return _to_blob([0.0] * EMBEDDING_DIM), 0
    mat = np.stack([np.frombuffer(r["embedding"], dtype="<f4") for r in rows])
    avg = _l2_normalize_np(mat.mean(axis=0))
    return avg.tobytes(), len(rows)


# ---------- 人物 CRUD ----------

def enroll_person(
    name: str, embedding: Any, now: str, unit: str = "", title: str = ""
) -> int:
    """登记一个新人物（首条样本）。返回自增 person id。

    embedding：该样本的声纹向量（一维序列）。人物的平均向量此时即该样本归一化值。
    """
    emb = _l2_normalize_np(np.asarray(embedding, dtype="<f4").reshape(-1))
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO voiceprint_people
                (name, unit, title, embedding, sample_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, unit, title, emb.tobytes(), 0, now, now),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO voiceprint_samples (person_id, embedding, created_at) VALUES (?, ?, ?)",
            (pid, _to_blob(embedding), now),
        )
        blob, count = _recompute_average(conn, pid)
        conn.execute(
            "UPDATE voiceprint_people SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
            (blob, count, now, pid),
        )
        return pid


def add_sample(person_id: int, embedding: Any, now: str) -> int | None:
    """为既有人物追加一条样本，并重算其平均向量。返回样本 id；人物不存在返回 None。"""
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM voiceprint_people WHERE id=?", (person_id,)
        ).fetchone()
        if exists is None:
            return None
        cur = conn.execute(
            "INSERT INTO voiceprint_samples (person_id, embedding, created_at) VALUES (?, ?, ?)",
            (person_id, _to_blob(embedding), now),
        )
        sid = int(cur.lastrowid)
        blob, count = _recompute_average(conn, person_id)
        conn.execute(
            "UPDATE voiceprint_people SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
            (blob, count, now, person_id),
        )
        return sid


def delete_sample(sample_id: int, now: str) -> bool:
    """删除一条样本并重算所属人物平均向量。返回是否命中。

    删空该人物最后一条样本时，人物行仍保留（sample_count=0、向量清零），
    是否连人物一起删由调用方决定（通常提示用户或调 delete_person）。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT person_id FROM voiceprint_samples WHERE id=?", (sample_id,)
        ).fetchone()
        if row is None:
            return False
        person_id = int(row["person_id"])
        conn.execute("DELETE FROM voiceprint_samples WHERE id=?", (sample_id,))
        blob, count = _recompute_average(conn, person_id)
        conn.execute(
            "UPDATE voiceprint_people SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
            (blob, count, now, person_id),
        )
        return True


def rename(person_id: int, name: str, now: str, unit: str | None = None, title: str | None = None) -> bool:
    """改人物姓名（可选单位/职务）。unit/title 传 None 表示不改。返回是否命中。"""
    sets = ["name=?", "updated_at=?"]
    params: list[Any] = [name, now]
    if unit is not None:
        sets.insert(1, "unit=?")
        params.insert(1, unit)
    if title is not None:
        # 插到 updated_at 之前
        sets.insert(-1, "title=?")
        params.insert(len(params) - 1, title)
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE voiceprint_people SET {', '.join(sets)} WHERE id=?",
            (*params, person_id),
        )
        return cur.rowcount > 0


def delete_person(person_id: int) -> bool:
    """删除人物及其全部样本（ON DELETE CASCADE）。返回是否命中。"""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM voiceprint_people WHERE id=?", (person_id,))
        return cur.rowcount > 0


def list_people() -> list[dict]:
    """列出全部登记人物（不含向量），供前端声纹库展示。按姓名、id 排序。

    返回 [{id, name, unit, title, sample_count, created_at, updated_at}]。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, unit, title, sample_count, created_at, updated_at "
            "FROM voiceprint_people ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "unit": r["unit"],
            "title": r["title"],
            "sample_count": r["sample_count"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def get_person(person_id: int) -> dict | None:
    """取单个人物（含解码后的平均向量）；不存在返回 None。"""
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, unit, title, embedding, sample_count, created_at, updated_at "
            "FROM voiceprint_people WHERE id=?",
            (person_id,),
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r["id"],
        "name": r["name"],
        "unit": r["unit"],
        "title": r["title"],
        "embedding": _from_blob(r["embedding"]),
        "sample_count": r["sample_count"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_all_embeddings() -> list[dict]:
    """取全部人物的平均声纹向量，供转写后匹配。

    返回 [{id, name, embedding:[...]}, ...]，**跳过 sample_count=0（无有效样本）**
    的人物，避免用清零向量参与匹配。可直接喂给 services.match_speakers。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, embedding, sample_count FROM voiceprint_people "
            "WHERE sample_count > 0"
        ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "embedding": _from_blob(r["embedding"])}
        for r in rows
    ]


def find_person_by_name(name: str) -> dict | None:
    """按姓名精确查找人物（大小写不敏感）；不存在返回 None。

    返回格式与 get_person 一致（含解码后的平均向量）。
    """
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, unit, title, embedding, sample_count, created_at, updated_at "
            "FROM voiceprint_people WHERE name COLLATE NOCASE=?",
            (name.strip(),),
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r["id"],
        "name": r["name"],
        "unit": r["unit"],
        "title": r["title"],
        "embedding": _from_blob(r["embedding"]),
        "sample_count": r["sample_count"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def list_samples(person_id: int) -> list[dict]:
    """列出某人物的样本元信息（不含向量），供前端展示/删除。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at FROM voiceprint_samples WHERE person_id=? ORDER BY id",
            (person_id,),
        ).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"]} for r in rows]
