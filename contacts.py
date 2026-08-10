"""通讯录的 SQLite 持久化层（副作用封装）。

「通讯录」= 常用联系人目录；每个联系人可选地带一份声纹（用于开会自动识别发言人）。
本模块由早先的 voiceprints.py 升级而来：表/文件/路由对外统一叫 contacts，
但**声纹匹配的内部机制（sv_embed / services.match_speakers / SV_* 与
VOICEPRINT_* 匹配参数）仍保留 voiceprint 命名**——声纹确实是 voiceprint，
把「声纹阈值」改叫「通讯录阈值」并不准确。

- 两表：
  - contacts：每个联系人一行，含姓名/单位/职务 + **平均后**的声纹向量
    （由其全部样本求均值再 L2 归一化）+ sample_count（冗余，便于列表展示）。
    **允许无声纹联系人**：仅录入姓名/单位/职务时 embedding 存全零占位、
    sample_count=0；`get_all_embeddings()` 以 sample_count>0 过滤，故无声纹的人
    天然不参与匹配。
  - contact_samples：每条声纹样本一行（contact_id 外键 + 原始声纹向量）。
    保留每条样本向量，使“删除某条样本后重算平均”“一人多样本”成立；
    但**不保留原始音频**（提完向量即丢，见 app.py 登记接口）。
- 向量以 float32 小端字节存 BLOB（numpy tobytes），读回 reshape 成一维 list。
- 每请求新建连接；WAL + busy_timeout（与 db.py 一致）。
- 所有函数为同步实现，在 app.py 中用 run_in_threadpool 包裹，避免阻塞事件循环。
- 联系人主键为自增 id（**非姓名**）：允许重名的不同人各自独立登记。
- **幂等迁移**：旧库若存在 voiceprint_people / voiceprint_samples 表，
  init_db 首次运行时 ALTER TABLE RENAME 为新表名，现网数据零丢失。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

import numpy as np

from services import env_int

# SQLite 文件路径。优先读 CONTACTS_DB_PATH；为兼容现网旧配置，回落到
# VOICEPRINT_DB_PATH，最后才用默认 data/contacts.db。
# 注意：默认文件名从 voiceprints.db 改为 contacts.db，但现网若已有
# voiceprints.db，应在 .env 里以 CONTACTS_DB_PATH（或旧的 VOICEPRINT_DB_PATH）
# 显式指向它，避免另起空库。
def _resolve_db_path() -> str:
    explicit = os.environ.get("CONTACTS_DB_PATH") or os.environ.get("VOICEPRINT_DB_PATH")
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "contacts.db")


CONTACTS_DB_PATH = _resolve_db_path()

# 声纹向量维度（CAM++ zh-cn 16k common = 192）。仅用于读回时 reshape 校验。
EMBEDDING_DIM = env_int("VOICEPRINT_EMBEDDING_DIM", 192)


def _connect() -> sqlite3.Connection:
    """新建一个配置好的连接：行工厂为 Row，开启 WAL 与写锁等待、外键。"""
    conn = sqlite3.connect(CONTACTS_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _migrate_legacy_tables(conn: sqlite3.Connection) -> None:
    """幂等迁移：把旧的 voiceprint_* 表改名为 contact_*（若存在且新表尚未建立）。

    仅在「旧表在、新表不在」时执行改名——已迁移过或全新库都跳过，可安全反复调用。
    样本表还需把外键列 person_id 改名为 contact_id（旧库列名遗留），否则后续按
    contact_id 建索引/查询会失败。
    """
    if _table_exists(conn, "voiceprint_people") and not _table_exists(conn, "contacts"):
        conn.execute("ALTER TABLE voiceprint_people RENAME TO contacts")
    if _table_exists(conn, "voiceprint_samples") and not _table_exists(conn, "contact_samples"):
        conn.execute("ALTER TABLE voiceprint_samples RENAME TO contact_samples")
    # 列改名：旧样本表用 person_id，新约定用 contact_id（SQLite 3.25+ 支持 RENAME COLUMN）
    if _table_exists(conn, "contact_samples") and _column_exists(conn, "contact_samples", "person_id"):
        conn.execute("ALTER TABLE contact_samples RENAME COLUMN person_id TO contact_id")


def init_db() -> None:
    """建表 + 索引（服务启动时调用）。先做旧表改名迁移，再 CREATE IF NOT EXISTS。"""
    os.makedirs(os.path.dirname(CONTACTS_DB_PATH) or ".", exist_ok=True)
    with _connect() as conn:
        _migrate_legacy_tables(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
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
            CREATE TABLE IF NOT EXISTS contact_samples (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                embedding  BLOB    NOT NULL,
                created_at TEXT    NOT NULL,
                FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_samples_contact "
            "ON contact_samples(contact_id)"
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


def _zero_embedding() -> bytes:
    """全零向量占位（EMBEDDING_DIM 维）——无声纹联系人 / 删空样本后使用。"""
    return _to_blob([0.0] * EMBEDDING_DIM)


def _recompute_average(conn: sqlite3.Connection, contact_id: int) -> tuple[bytes, int]:
    """由某联系人全部样本重算平均声纹（均值后 L2 归一化）。返回 (blob, sample_count)。

    无样本时返回全零向量（EMBEDDING_DIM 维）与 0——该联系人仍作为「无声纹」保留。
    """
    rows = conn.execute(
        "SELECT embedding FROM contact_samples WHERE contact_id=?", (contact_id,)
    ).fetchall()
    if not rows:
        return _zero_embedding(), 0
    mat = np.stack([np.frombuffer(r["embedding"], dtype="<f4") for r in rows])
    avg = _l2_normalize_np(mat.mean(axis=0))
    return avg.tobytes(), len(rows)


# ---------- 联系人 CRUD ----------

def add_contact(name: str, now: str, unit: str = "", title: str = "") -> int:
    """新增一个**无声纹**联系人（仅姓名/单位/职务）。返回自增 contact id。

    embedding 存全零占位、sample_count=0，故不参与声纹匹配；之后可通过
    add_sample / 转写行标注为其补录声纹。
    """
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO contacts
                (name, unit, title, embedding, sample_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, unit, title, _zero_embedding(), 0, now, now),
        )
        return int(cur.lastrowid)


def enroll_person(
    name: str, embedding: Any, now: str, unit: str = "", title: str = ""
) -> int:
    """登记一个带首条声纹样本的联系人。返回自增 contact id。

    embedding：该样本的声纹向量（一维序列）。联系人的平均向量此时即该样本归一化值。
    """
    emb = _l2_normalize_np(np.asarray(embedding, dtype="<f4").reshape(-1))
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO contacts
                (name, unit, title, embedding, sample_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (name, unit, title, emb.tobytes(), 0, now, now),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO contact_samples (contact_id, embedding, created_at) VALUES (?, ?, ?)",
            (pid, _to_blob(embedding), now),
        )
        blob, count = _recompute_average(conn, pid)
        conn.execute(
            "UPDATE contacts SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
            (blob, count, now, pid),
        )
        return pid


def add_sample(contact_id: int, embedding: Any, now: str) -> int | None:
    """为既有联系人追加一条声纹样本，并重算其平均向量。返回样本 id；联系人不存在返回 None。"""
    with _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM contacts WHERE id=?", (contact_id,)
        ).fetchone()
        if exists is None:
            return None
        cur = conn.execute(
            "INSERT INTO contact_samples (contact_id, embedding, created_at) VALUES (?, ?, ?)",
            (contact_id, _to_blob(embedding), now),
        )
        sid = int(cur.lastrowid)
        blob, count = _recompute_average(conn, contact_id)
        conn.execute(
            "UPDATE contacts SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
            (blob, count, now, contact_id),
        )
        return sid


def delete_sample(sample_id: int, now: str) -> bool:
    """删除一条声纹样本并重算所属联系人平均向量。返回是否命中。

    删空该联系人最后一条样本时，联系人行仍保留（sample_count=0、向量清零、回落为
    「无声纹联系人」），是否连人一起删由调用方决定（通常提示用户或调 delete_person）。
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT contact_id FROM contact_samples WHERE id=?", (sample_id,)
        ).fetchone()
        if row is None:
            return False
        contact_id = int(row["contact_id"])
        conn.execute("DELETE FROM contact_samples WHERE id=?", (sample_id,))
        blob, count = _recompute_average(conn, contact_id)
        conn.execute(
            "UPDATE contacts SET embedding=?, sample_count=?, updated_at=? WHERE id=?",
            (blob, count, now, contact_id),
        )
        return True


def rename(contact_id: int, name: str, now: str, unit: str | None = None, title: str | None = None) -> bool:
    """改联系人姓名（可选单位/职务）。unit/title 传 None 表示不改。返回是否命中。"""
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
            f"UPDATE contacts SET {', '.join(sets)} WHERE id=?",
            (*params, contact_id),
        )
        return cur.rowcount > 0


def delete_person(contact_id: int) -> bool:
    """删除联系人及其全部声纹样本（ON DELETE CASCADE）。返回是否命中。"""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        return cur.rowcount > 0


def list_people() -> list[dict]:
    """列出全部联系人（不含向量），供前端通讯录展示。按姓名、id 排序。

    返回 [{id, name, unit, title, sample_count, created_at, updated_at}]。
    has_voice 由调用方（app.py）据 sample_count>0 补齐，避免本层引入展示语义。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, unit, title, sample_count, created_at, updated_at "
            "FROM contacts ORDER BY name COLLATE NOCASE, id"
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


def get_person(contact_id: int) -> dict | None:
    """取单个联系人（含解码后的平均向量）；不存在返回 None。"""
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, unit, title, embedding, sample_count, created_at, updated_at "
            "FROM contacts WHERE id=?",
            (contact_id,),
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
    """取全部联系人的平均声纹向量，供转写后匹配。

    返回 [{id, name, embedding:[...]}, ...]，**跳过 sample_count=0（无有效样本）**
    的联系人，避免用清零向量参与匹配。可直接喂给 services.match_speakers。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, embedding, sample_count FROM contacts "
            "WHERE sample_count > 0"
        ).fetchall()
    return [
        {"id": r["id"], "name": r["name"], "embedding": _from_blob(r["embedding"])}
        for r in rows
    ]


def find_person_by_name(name: str) -> dict | None:
    """按姓名精确查找联系人（大小写不敏感）；不存在返回 None。

    返回格式与 get_person 一致（含解码后的平均向量）。
    """
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, name, unit, title, embedding, sample_count, created_at, updated_at "
            "FROM contacts WHERE name COLLATE NOCASE=?",
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


def list_samples(contact_id: int) -> list[dict]:
    """列出某联系人的声纹样本元信息（不含向量），供前端展示/删除。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at FROM contact_samples WHERE contact_id=? ORDER BY id",
            (contact_id,),
        ).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"]} for r in rows]
