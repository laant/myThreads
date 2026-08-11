"""각 글을 카테고리에 배정하고 한 줄 요약·태그를 붙인다."""
from __future__ import annotations

import json
import logging

from ..db import db, is_cancel_requested, now, touch_job
from . import llm, taxonomy

log = logging.getLogger("classify")

BATCH = 15
SYSTEM = """너는 개인 지식 큐레이터다. 주어진 카테고리 체계에 맞춰 글을 분류한다.
규칙:
- category 는 반드시 주어진 slug 중 하나. 확실치 않으면 "기타"에 해당하는 slug.
- summary 는 저장한 사람이 나중에 목록에서 훑어볼 한 줄(40자 이내, 한국어, 서술형 금지·핵심만).
- tags 는 2~5개, 한국어 명사 위주.
- confidence 는 0~1.
반드시 JSON만 출력한다."""


def _prompt(cats: list[dict], posts: list[dict]) -> str:
    cat_lines = "\n".join(f"- {c['slug']}: {c['name']} — {c['description']}" for c in cats)
    post_lines = []
    for p in posts:
        text = (p["full_text"] or p["body"] or "").strip()
        img = f" (이미지 {p['n_media']}장)" if p["n_media"] else ""
        post_lines.append(f"### id={p['id']} @{p['author']}{img}\n{text[:1500]}")
    return f"""카테고리 목록:
{cat_lines}

아래 글들을 분류해라.

{chr(10).join(post_lines)}

출력 형식:
{{"assignments": [
  {{"id": "글id", "category": "slug", "secondary": "slug 또는 null",
    "confidence": 0.0, "summary": "한 줄 요약", "tags": ["태그1","태그2"]}}
]}}"""


def _pending(only_unclassified: bool) -> list[dict]:
    q = """
      SELECT p.id, p.author, p.body, p.full_text,
             (SELECT COUNT(*) FROM media m WHERE m.post_id = p.id) AS n_media
      FROM posts p
      LEFT JOIN classification c ON c.post_id = p.id
      WHERE 1=1 {cond}
      ORDER BY p.saved_at DESC
    """
    cond = "AND c.post_id IS NULL" if only_unclassified else "AND COALESCE(c.locked,0)=0"
    with db() as conn:
        return [dict(r) for r in conn.execute(q.format(cond=cond))]


def run(only_unclassified: bool = True, rebuild_taxonomy: bool = False,
        job_id: int | None = None) -> dict:
    cats = taxonomy.build(force=rebuild_taxonomy)
    if not cats:
        return {"classified": 0, "reason": "카테고리 없음"}
    by_slug = {c["slug"]: c for c in cats}
    fallback = next((c for c in cats if c["name"] == "기타"), cats[-1])

    posts = _pending(only_unclassified)
    if not posts:
        log.info("분류할 글이 없습니다.")
        return {"classified": 0}

    model = llm.resolve_model()
    done = 0
    for i in range(0, len(posts), BATCH):
        batch = posts[i:i + BATCH]
        try:
            data = llm.ask_json(SYSTEM, _prompt(cats, batch), max_tokens=4000)
        except Exception as exc:
            log.error("분류 배치 실패 (%d~%d): %s", i, i + len(batch), exc)
            continue
        rows = data.get("assignments") or []
        got = {r.get("id") for r in rows}
        for r in rows:
            cat = by_slug.get(r.get("category")) or fallback
            sec = by_slug.get(r.get("secondary")) if r.get("secondary") else None
            _save(r.get("id"), cat, sec, r, model)
            done += 1
        for p in batch:  # 응답에서 누락된 글은 기타로
            if p["id"] not in got:
                _save(p["id"], fallback, None,
                      {"confidence": 0.2, "summary": "", "tags": []}, model)
                done += 1
        seen_n = min(i + BATCH, len(posts))
        log.info("분류 진행 %d/%d", seen_n, len(posts))
        if job_id:
            touch_job(job_id, f"분류 {seen_n}/{len(posts)}건")
        if job_id and is_cancel_requested(job_id):
            log.warning("중단 요청 — 분류를 여기서 멈춥니다 (%d건 완료)", done)
            break
    return {"classified": done, "categories": len(cats), "model": model}


# 태그로서 의미가 없는 단어 (내용이 아니라 형식을 가리키는 말)
TAG_STOPWORDS = {"댓글", "답글", "리플", "comment", "reply", "스레드", "thread", "게시글", "post"}


def _clean_tags(tags) -> list[str]:
    out = []
    for t in tags or []:
        t = str(t).strip().lstrip("#")
        if not t or t.lower() in TAG_STOPWORDS:
            continue
        if t not in out:
            out.append(t)
    return out[:5]


def _save(post_id: str | None, cat: dict, sec: dict | None, r: dict, model: str) -> None:
    if not post_id:
        return
    with db() as conn:
        exists = conn.execute(
            "SELECT locked FROM classification WHERE post_id=?", (post_id,)
        ).fetchone()
        if exists and exists["locked"]:
            return
        conn.execute(
            """INSERT INTO classification(post_id,category_id,secondary_id,confidence,summary,
                     tags,model,method,locked,updated_at)
               VALUES(?,?,?,?,?,?,?,'ai',0,?)
               ON CONFLICT(post_id) DO UPDATE SET
                 category_id=excluded.category_id, secondary_id=excluded.secondary_id,
                 confidence=excluded.confidence, summary=excluded.summary, tags=excluded.tags,
                 model=excluded.model, method='ai', updated_at=excluded.updated_at""",
            (post_id, cat["id"], sec["id"] if sec else None,
             float(r.get("confidence") or 0), (r.get("summary") or "")[:200],
             json.dumps(_clean_tags(r.get("tags")), ensure_ascii=False), model, now()),
        )
