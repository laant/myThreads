"""FastAPI 웹 UI + API."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import Request

from . import config
from .classifier import llm
from .db import clear_stale_jobs, db, delete_post, init_db, now, restore_deleted

BASE = Path(__file__).parent
app = FastAPI(title="myThreads", docs_url=None, redoc_url=None)


def _asset_version() -> str:
    """정적 파일이 바뀌면 값이 달라진다 → 브라우저 캐시 무효화 + 새 버전 감지용."""
    stamp = 0.0
    for p in (BASE / "static" / "app.js", BASE / "static" / "styles.css",
              BASE / "templates" / "index.html"):
        try:
            stamp = max(stamp, p.stat().st_mtime)
        except OSError:
            pass
    return str(int(stamp))


ASSET_V = _asset_version()

init_db()


@app.middleware("http")
async def no_cache_api(request: Request, call_next):
    """API 응답은 절대 캐시하지 않는다 (동기화 중 옛 데이터가 보이는 것 방지)."""
    response = await call_next(request)
    if request.url.path.startswith("/api") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(config.MEDIA_DIR)), name="media")


# ── 조회 ────────────────────────────────────────────────────────────────

def _categories() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM classification x WHERE x.category_id=c.id) AS count
               FROM categories c ORDER BY c.sort, c.id"""
        ).fetchall()
    return [dict(r) for r in rows]


def _tag_counts(category: str | None = None) -> list[dict]:
    """지금 보고 있는 화면에 실제로 있는 태그를, 그 화면 기준 개수로 센다.

    전체 개수로 보여주면 카테고리를 보는 중에 '일상 117' 같은 태그를 눌러도
    한 건도 안 나오는 일이 생긴다 — 목록은 카테고리 안으로 한정돼 있기 때문이다.
    그래서 카테고리를 고르면 그 안의 태그만, 그 안의 개수로 센다.
    """
    if category == "unclassified":
        return []                      # 분류가 없으니 태그도 없다
    sql = ["SELECT cl.tags FROM classification cl", "JOIN posts p ON p.id = cl.post_id"]
    params: list = []
    if category and category != "all":
        sql += ["LEFT JOIN categories cat ON cat.id = cl.category_id",
                "LEFT JOIN categories sec ON sec.id = cl.secondary_id",
                "WHERE cat.slug = ? OR sec.slug = ?"]
        params += [category, category]

    counts: dict[str, int] = {}
    with db() as conn:
        for r in conn.execute(" ".join(sql), params):
            try:
                for t in json.loads(r["tags"] or "[]"):
                    counts[t] = counts.get(t, 0) + 1
            except Exception:
                pass
    # 동점이면 가나다순 — 같은 횟수인데 어떤 건 보이고 어떤 건 안 보이는 일이 없도록
    ordered = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [{"name": t, "count": c} for t, c in ordered]


SORTS = {
    # 작성일 기준 최신순 — 날짜를 모르는 글은 뒤로
    "newest": "COALESCE(NULLIF(p.posted_at,0), 0) DESC, p.saved_at DESC",
    "oldest": "COALESCE(NULLIF(p.posted_at,0), 9999999999) ASC, p.saved_at ASC",
    # 저장한 순서 (0 = 가장 최근에 저장)
    "saved": "CASE WHEN p.saved_rank IS NULL THEN 1 ELSE 0 END, p.saved_rank ASC, "
             "p.saved_at DESC",
    "author": "p.author ASC, COALESCE(p.posted_at,0) DESC",
}


def _posts(category: str | None = None, q: str | None = None, tag: str | None = None,
           limit: int = 500, offset: int = 0, post_id: str | None = None,
           sort: str = "newest") -> list[dict]:
    where, params = ["1=1"], []
    # 카테고리를 볼 때, 보조분류로만 걸린 글은 '관련 글'로 따로 표시한다.
    # (주분류가 다른 글이라 섞어 놓으면 남의 카테고리 글이 끼어든 것처럼 보인다)
    related_expr, sel_params = "0 AS related", []
    if post_id:
        where.append("p.id = ?")
        params.append(post_id)
    if category and category != "all":
        if category == "unclassified":
            where.append("cl.post_id IS NULL")
        else:
            where.append("(cat.slug = ? OR sec.slug = ?)")
            params += [category, category]
            related_expr = "CASE WHEN cat.slug = ? THEN 0 ELSE 1 END AS related"
            sel_params.append(category)
    if q:
        where.append("(p.full_text LIKE ? OR p.author LIKE ? OR cl.summary LIKE ? OR cl.tags LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like, like]
    if tag:
        where.append("cl.tags LIKE ?")
        params.append(f'%"{tag}"%')

    sql = f"""
      SELECT p.*, cl.summary, cl.tags, cl.confidence, cl.method, cl.locked,
             cat.slug AS cat_slug, cat.name AS cat_name, cat.color AS cat_color,
             sec.slug AS sec_slug, sec.name AS sec_name,
             {related_expr}
      FROM posts p
      LEFT JOIN classification cl ON cl.post_id = p.id
      LEFT JOIN categories cat ON cat.id = cl.category_id
      LEFT JOIN categories sec ON sec.id = cl.secondary_id
      WHERE {' AND '.join(where)}
      ORDER BY related, {SORTS.get(sort, SORTS['newest'])}, p.id DESC
      LIMIT ? OFFSET ?
    """
    params = sel_params + params + [limit, offset]
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
        ids = [r["id"] for r in rows]
        media, segs = {}, {}
        if ids:
            marks = ",".join("?" * len(ids))
            for m in conn.execute(
                f"SELECT * FROM media WHERE post_id IN ({marks}) ORDER BY post_id, ord", ids
            ):
                media.setdefault(m["post_id"], []).append(
                    {"kind": m["kind"], "url": m["url"], "local": m["local_path"], "alt": m["alt"]}
                )
            for s in conn.execute(
                f"SELECT * FROM segments WHERE post_id IN ({marks}) ORDER BY post_id, ord", ids
            ):
                segs.setdefault(s["post_id"], []).append(
                    {"kind": s["kind"], "text": s["text"], "posted_at": s["posted_at"]}
                )
    for r in rows:
        r["media"] = media.get(r["id"], [])
        r["segments"] = segs.get(r["id"], [])
        try:
            r["tags"] = json.loads(r["tags"]) if r["tags"] else []
        except Exception:
            r["tags"] = []
        r.pop("raw", None)
    return rows


@app.get("/")
def index():
    html = (BASE / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__V__", ASSET_V))


@app.get("/api/status")
def api_status():
    """가벼운 상태 조회 — 동기화 중 몇 초마다 부르는 용도."""
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    return {"total": total, "job": dict(job) if job else None, "version": ASSET_V}


@app.get("/api/bootstrap")
def bootstrap():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        unclassified = conn.execute(
            "SELECT COUNT(*) c FROM posts p LEFT JOIN classification cl ON cl.post_id=p.id "
            "WHERE cl.post_id IS NULL"
        ).fetchone()["c"]
        job = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "categories": _categories(),
        "total": total,
        "unclassified": unclassified,
        "tags": _tag_counts(),
        "job": dict(job) if job else None,
        "sync_times": config.SYNC_TIMES,
        "logged_in": config.STATE_PATH.exists(),
        "version": ASSET_V,
        "provider": config.LLM_PROVIDER,
        "llm_ready": llm.has_key(),
        "llm_key_env": llm.key_env_name(),
    }


@app.get("/api/tags")
def api_tags(category: str | None = None):
    """지금 보고 있는 카테고리 안의 태그 목록 (많이 쓰인 순, 전부)."""
    return {"tags": _tag_counts(category)}


@app.get("/api/posts")
def api_posts(category: str | None = None, q: str | None = None, tag: str | None = None,
              limit: int = 500, offset: int = 0, sort: str = "newest"):
    return {"posts": _posts(category, q, tag, limit, offset, sort=sort)}


@app.get("/api/posts/{post_id}")
def api_post(post_id: str):
    found = _posts(limit=1, post_id=post_id)
    if not found:
        raise HTTPException(404, "글을 찾을 수 없습니다.")
    return found[0]


# ── 변경 ────────────────────────────────────────────────────────────────

class CategoryPatch(BaseModel):
    category_id: int | None = None
    lock: bool = True


@app.post("/api/posts/{post_id}/category")
def set_category(post_id: str, body: CategoryPatch):
    with db() as conn:
        conn.execute(
            """INSERT INTO classification(post_id,category_id,method,locked,updated_at)
               VALUES(?,?,'manual',?,?)
               ON CONFLICT(post_id) DO UPDATE SET
                 category_id=excluded.category_id, method='manual',
                 locked=excluded.locked, updated_at=excluded.updated_at""",
            (post_id, body.category_id, int(body.lock), now()),
        )
    return {"ok": True}


@app.delete("/api/posts/{post_id}")
def api_delete_post(post_id: str, forget: bool = True):
    """저장해 둔 글을 로컬에서만 지운다.

    내려받은 이미지 파일까지 함께 지우며, Threads 계정의 '저장됨' 목록은 그대로다.
    forget=false 로 부르면 다음 수집 때 다시 들어온다.
    """
    res = delete_post(post_id, forget=forget)
    if not res.get("ok"):
        raise HTTPException(404, "글을 찾을 수 없습니다.")
    return res


@app.post("/api/posts/{post_id}/restore")
def api_restore_post(post_id: str):
    """'지운 글' 기억만 지운다 — 이후 전체 훑기에서 다시 수집된다."""
    return {"ok": True, "restored": restore_deleted(post_id)}


class ViewPatch(BaseModel):
    view: str


@app.post("/api/categories/{cat_id}/view")
def set_view(cat_id: int, body: ViewPatch):
    if body.view not in ("card", "table", "board"):
        raise HTTPException(400, "view 값이 올바르지 않습니다.")
    with db() as conn:
        conn.execute("UPDATE categories SET view=? WHERE id=?", (body.view, cat_id))
    return {"ok": True}


class CategoryEdit(BaseModel):
    name: str | None = None
    description: str | None = None


@app.post("/api/categories/{cat_id}")
def edit_category(cat_id: int, body: CategoryEdit):
    with db() as conn:
        if body.name:
            conn.execute("UPDATE categories SET name=? WHERE id=?", (body.name, cat_id))
        if body.description is not None:
            conn.execute("UPDATE categories SET description=? WHERE id=?",
                         (body.description, cat_id))
    return {"ok": True}


def _enqueue(kind: str) -> dict:
    # 죽은 채로 '실행 중'에 걸려 있는 작업이 새 작업을 막지 않도록 먼저 정리
    cleared = clear_stale_jobs(config.STALE_JOB_MIN)
    with db() as conn:
        running = conn.execute(
            "SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if running:
            return {"ok": False, "message": "이미 실행 중인 작업이 있습니다.",
                    "job_id": running["id"]}
        cur = conn.execute(
            "INSERT INTO jobs(kind,status,started_at,heartbeat_at) VALUES(?,'queued',?,?)",
            (kind, now(), now()))
        return {"ok": True, "job_id": cur.lastrowid, "cleared": cleared}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    """진행 중인 작업에 중단을 요청한다. 이미 저장된 글은 그대로 남는다."""
    with db() as conn:
        conn.execute(
            "UPDATE jobs SET status='canceled', message='중단 요청됨', heartbeat_at=? "
            "WHERE id=? AND status IN ('queued','running')",
            (now(), job_id))
    return {"ok": True}


@app.post("/api/sync")
def api_sync(full: bool = False):
    return _enqueue("sync_full" if full else "sync")


@app.post("/api/reclassify")
def api_reclassify():
    return _enqueue("reclassify")


@app.post("/api/classify")
def api_classify():
    return _enqueue("classify")


@app.get("/api/jobs")
def api_jobs(limit: int = 10):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]
    return {"jobs": rows}


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"error": str(exc)})
