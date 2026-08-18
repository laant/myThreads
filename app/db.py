"""SQLite 스키마 및 접근 헬퍼."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import DB_PATH, MEDIA_DIR

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,   -- Threads shortcode
    pk            TEXT,
    url           TEXT,
    author        TEXT,
    author_name   TEXT,
    author_pic    TEXT,
    posted_at     INTEGER,
    body          TEXT,               -- 루트 본문
    thread_text   TEXT,               -- 작성자가 이어서 쓴 댓글을 이어붙인 전문
    full_text     TEXT,               -- body + thread_text (검색/분류용)
    like_count    INTEGER DEFAULT 0,
    reply_count   INTEGER DEFAULT 0,
    saved_at      INTEGER,            -- 저장됨 목록에서 처음 발견한 시각
    saved_rank    INTEGER,            -- 저장됨 목록에서의 위치 (0 = 가장 최근에 저장)
    reply_to      TEXT,               -- 이 글이 남의 글에 단 '댓글'이면 원글 작성자
    detail_ok     INTEGER DEFAULT 0,  -- 상세(댓글 이어쓰기) 수집 성공 여부
    raw           TEXT,
    created_at    INTEGER,
    updated_at    INTEGER
);

CREATE TABLE IF NOT EXISTS segments (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id   TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    kind      TEXT NOT NULL,          -- root | reply
    author    TEXT,
    text      TEXT,
    posted_at INTEGER,
    UNIQUE(post_id, ord)
);

CREATE TABLE IF NOT EXISTS media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id    TEXT NOT NULL,
    ord        INTEGER NOT NULL,
    kind       TEXT NOT NULL,         -- image | video
    url        TEXT,
    local_path TEXT,
    alt        TEXT,
    UNIQUE(post_id, ord)
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    color       TEXT,
    view        TEXT DEFAULT 'card',  -- 카테고리별 기본 보기 (card | table | board)
    sort        INTEGER DEFAULT 100,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS classification (
    post_id     TEXT PRIMARY KEY,
    category_id INTEGER,
    secondary_id INTEGER,
    confidence  REAL,
    summary     TEXT,
    tags        TEXT,                 -- JSON 배열
    model       TEXT,
    method      TEXT,                 -- ai | manual
    locked      INTEGER DEFAULT 0,    -- 1이면 자동 재분류에서 제외 (수동 확정)
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT,
    status       TEXT,                -- queued | running | done | error | canceled
    message      TEXT,
    stats        TEXT,
    started_at   INTEGER,
    finished_at  INTEGER,
    heartbeat_at INTEGER              -- 살아 있음을 알리는 마지막 시각
);

-- 저장하지 '않기로' 한 글도 기억해야 매번 다시 열어보지 않는다
CREATE TABLE IF NOT EXISTS skipped (
    id         TEXT PRIMARY KEY,
    reason     TEXT,               -- comment(남의 글에 단 댓글) | failed(수집 실패)
                                   -- | deleted(사용자가 로컬에서 지움)
    tries      INTEGER DEFAULT 1,
    author     TEXT,
    posted_at  INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_saved  ON posts(saved_at DESC);
CREATE INDEX IF NOT EXISTS idx_seg_post     ON segments(post_id);
CREATE INDEX IF NOT EXISTS idx_media_post   ON media(post_id);
CREATE INDEX IF NOT EXISTS idx_cls_category ON classification(category_id);
"""


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        # 기존 DB 마이그레이션 (컬럼 추가는 여기서)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "heartbeat_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at INTEGER")
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
        for col, decl in (("saved_rank", "INTEGER"), ("reply_to", "TEXT")):
            if col not in pcols:
                conn.execute(f"ALTER TABLE posts ADD COLUMN {col} {decl}")


def get_setting(key: str, default: Any = None) -> Any:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )


# ── 저장 헬퍼 ───────────────────────────────────────────────────────────

def upsert_post(conn: sqlite3.Connection, post: dict) -> bool:
    """저장하고 '신규 여부'를 반환."""
    exists = conn.execute("SELECT 1 FROM posts WHERE id=?", (post["id"],)).fetchone()
    ts = now()
    if exists:
        conn.execute(
            """UPDATE posts SET pk=?, url=?, author=?, author_name=?, author_pic=?,
                   posted_at=?, body=?, thread_text=?, full_text=?, like_count=?,
                   reply_count=?, detail_ok=?, reply_to=?,
                   saved_rank=COALESCE(?, saved_rank), raw=?, updated_at=?
               WHERE id=?""",
            (
                post.get("pk"), post.get("url"), post.get("author"), post.get("author_name"),
                post.get("author_pic"), post.get("posted_at"), post.get("body"),
                post.get("thread_text"), post.get("full_text"), post.get("like_count", 0),
                post.get("reply_count") or 0, int(post.get("detail_ok", 0)),
                post.get("reply_to") or None, post.get("saved_rank"),
                json.dumps(post.get("raw"), ensure_ascii=False) if post.get("raw") else None,
                ts, post["id"],
            ),
        )
        return False
    conn.execute(
        """INSERT INTO posts(id, pk, url, author, author_name, author_pic, posted_at, body,
               thread_text, full_text, like_count, reply_count, saved_at, saved_rank,
               reply_to, detail_ok, raw, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            post["id"], post.get("pk"), post.get("url"), post.get("author"),
            post.get("author_name"), post.get("author_pic"), post.get("posted_at"),
            post.get("body"), post.get("thread_text"), post.get("full_text"),
            post.get("like_count", 0), post.get("reply_count") or 0, ts,
            post.get("saved_rank"), post.get("reply_to") or None,
            int(post.get("detail_ok", 0)),
            json.dumps(post.get("raw"), ensure_ascii=False) if post.get("raw") else None,
            ts, ts,
        ),
    )
    return True


def replace_segments(conn: sqlite3.Connection, post_id: str, segments: Iterable[dict]) -> None:
    conn.execute("DELETE FROM segments WHERE post_id=?", (post_id,))
    for i, s in enumerate(segments):
        conn.execute(
            "INSERT INTO segments(post_id, ord, kind, author, text, posted_at) VALUES(?,?,?,?,?,?)",
            (post_id, i, s.get("kind", "reply"), s.get("author"), s.get("text"), s.get("posted_at")),
        )


def replace_media(conn: sqlite3.Connection, post_id: str, media: Iterable[dict]) -> None:
    conn.execute("DELETE FROM media WHERE post_id=?", (post_id,))
    for i, m in enumerate(media):
        conn.execute(
            "INSERT INTO media(post_id, ord, kind, url, local_path, alt) VALUES(?,?,?,?,?,?)",
            (post_id, i, m.get("kind", "image"), m.get("url"), m.get("local_path"), m.get("alt")),
        )


def mark_skipped(post_id: str, reason: str, author: str | None = None,
                 posted_at: int | None = None) -> None:
    """이번에 저장하지 않기로 한 글을 기록 (다음 실행에서 또 건드리지 않도록)."""
    if not post_id:
        return
    with db() as conn:
        conn.execute(
            """INSERT INTO skipped(id, reason, tries, author, posted_at, updated_at)
               VALUES(?,?,1,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 reason=excluded.reason, tries=skipped.tries+1,
                 author=COALESCE(excluded.author, skipped.author),
                 posted_at=COALESCE(excluded.posted_at, skipped.posted_at),
                 updated_at=excluded.updated_at""",
            (post_id, reason, author, posted_at, now()),
        )


def skipped_index(max_failed_tries: int = 3) -> tuple[set[str], set[str]]:
    """다시 건드리지 않을 글 목록.

    댓글과 '사용자가 지운 글'은 영구 제외, 수집 실패는 몇 번까지만 재시도.
    """
    ids: set[str] = set()
    keys: set[str] = set()
    with db() as conn:
        rows = conn.execute("SELECT id, reason, tries, author, posted_at FROM skipped")
        for r in rows:
            if r["reason"] in ("comment", "deleted") or int(r["tries"] or 0) >= max_failed_tries:
                ids.add(r["id"])
                if r["author"] and r["posted_at"]:
                    keys.add(f"{r['author'].strip().lower()}|{int(r['posted_at'])}")
    return ids, keys


# ── 로컬에서 지우기 ─────────────────────────────────────────────────────
# 여기서 지우는 것은 '내 컴퓨터에 받아둔 사본'뿐이다.
# 이 프로그램은 Threads에 아무것도 쓰지 않으므로 계정의 '저장됨' 목록은 그대로다.

def _remove_media_files(paths: Iterable[str | None]) -> int:
    """지운 글에만 딸린 이미지 파일을 정리한다.

    - 다른 글이 같은 파일을 함께 쓰고 있으면 남긴다
    - MEDIA_DIR 밖을 가리키는 경로는 무시한다 (DB 값이 이상해도 엉뚱한 파일을 지우지 않도록)
    """
    base = MEDIA_DIR.resolve()
    removed = 0
    with db() as conn:
        for p in paths:
            if not p:
                continue
            still_used = conn.execute(
                "SELECT 1 FROM media WHERE local_path=? LIMIT 1", (p,)).fetchone()
            if still_used:
                continue
            target = (MEDIA_DIR / str(p).split("/")[-1]).resolve()
            if target.parent != base or not target.is_file():
                continue
            try:
                target.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def delete_post(post_id: str, forget: bool = True) -> dict:
    """저장해 둔 글 1건을 로컬에서 지운다 (본문·이어쓴 글·분류·이미지 파일).

    forget=True 면 '내가 지운 글'로 기억해 다음 수집 때 다시 가져오지 않는다.
    (기억을 지우려면 restore_deleted() — 그 뒤 전체 훑기를 하면 다시 들어온다)
    """
    with db() as conn:
        row = conn.execute(
            "SELECT id, author, posted_at FROM posts WHERE id=?", (post_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        author, posted_at = row["author"], row["posted_at"]
        paths = [r["local_path"] for r in conn.execute(
            "SELECT local_path FROM media WHERE post_id=?", (post_id,))]
        for table, col in (("classification", "post_id"), ("segments", "post_id"),
                           ("media", "post_id"), ("posts", "id")):
            conn.execute(f"DELETE FROM {table} WHERE {col}=?", (post_id,))

    removed = _remove_media_files(paths)
    if forget:
        mark_skipped(post_id, "deleted", author, posted_at)
    return {"ok": True, "id": post_id, "media_removed": removed, "forgotten": bool(forget)}


def deleted_posts() -> list[dict]:
    """내가 지운 글 목록 (다시 가져오지 않도록 기억해 둔 것)."""
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, author, posted_at, updated_at FROM skipped "
            "WHERE reason='deleted' ORDER BY updated_at DESC")]


def restore_deleted(post_id: str | None = None) -> int:
    """'지운 글' 기억을 없앤다 — 다음 전체 훑기에서 다시 수집된다."""
    with db() as conn:
        if post_id:
            cur = conn.execute(
                "DELETE FROM skipped WHERE id=? AND reason='deleted'", (post_id,))
        else:
            cur = conn.execute("DELETE FROM skipped WHERE reason='deleted'")
        return cur.rowcount


def start_job(kind: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO jobs(kind,status,started_at,heartbeat_at) VALUES(?,?,?,?)",
            (kind, "running", now(), now()),
        )
        return int(cur.lastrowid)


def touch_job(job_id: int, message: str = "", stats: dict | None = None) -> None:
    """작업이 살아 있음을 알리고 진행 상황을 남긴다."""
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET heartbeat_at=?, message=COALESCE(NULLIF(?,''),message), "
            "stats=COALESCE(?,stats) WHERE id=?",
            (now(), message[:2000],
             json.dumps(stats, ensure_ascii=False) if stats is not None else None, job_id),
        )


def finish_job(job_id: int, status: str, message: str = "", stats: dict | None = None) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, message=?, stats=?, finished_at=?, heartbeat_at=? "
            "WHERE id=?",
            (status, message[:2000], json.dumps(stats or {}, ensure_ascii=False),
             now(), now(), job_id),
        )


def is_cancel_requested(job_id: int) -> bool:
    with db() as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row) and row["status"] == "canceled"


def reset_running_jobs() -> int:
    """worker가 새로 뜰 때 호출 — 이전 프로세스가 남긴 '실행 중'은 모두 죽은 작업이다."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='error', finished_at=?, "
            "message=COALESCE(message,'') || ' (worker 재시작으로 중단됨)' "
            "WHERE status='running'",
            (now(),),
        )
        return cur.rowcount


def clear_stale_jobs(stale_minutes: int) -> int:
    """컨테이너 재시작 등으로 죽어버린 '실행 중' 작업을 정리한다."""
    cutoff = now() - stale_minutes * 60
    with db() as conn:
        cur = conn.execute(
            """UPDATE jobs SET status='error', finished_at=?,
                   message=COALESCE(message,'') || ' (응답이 끊겨 자동 정리됨)'
               WHERE status IN ('running','queued')
                 AND COALESCE(heartbeat_at, started_at, 0) < ?""",
            (now(), cutoff),
        )
        return cur.rowcount
