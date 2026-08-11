"""저장된 글 전체를 훑어 '나만의 카테고리 체계'를 자동으로 만들어낸다."""
from __future__ import annotations

import logging
import re

from ..db import db, now
from . import llm

log = logging.getLogger("taxonomy")

PALETTE = [
    "#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
    "#14b8a6", "#f43f5e", "#84cc16", "#06b6d4", "#a855f7", "#eab308",
    "#3b82f6", "#22c55e", "#fb7185", "#94a3b8",
]

SYSTEM = """너는 개인 지식 큐레이터다. 사용자가 Threads에서 '저장'해 둔 글들을 보고,
그 사람이 실제로 다시 찾아볼 때 편한 분류 체계를 설계한다.

원칙:
- 카테고리는 6~12개. 너무 잘게 쪼개지 말 것.
- 글의 '형식'이 아니라 '왜 저장했는지(용도/주제)'를 기준으로 나눈다.
- 실제로 저장된 글이 3건 미만인 주제는 만들지 않는다.
- 어디에도 안 맞는 글을 위해 마지막에 '기타'를 하나 둔다.
- 이름은 한국어로 짧고 구체적으로 (예: "마케팅 인사이트", "개발 팁", "글감·문장").
- view 는 그 카테고리를 볼 때 편한 방식을 고른다:
  "card"(본문 읽기 위주) / "table"(목록·비교·정보성) / "board"(이미지 위주).
반드시 JSON만 출력한다."""

TEMPLATE = """아래는 사용자가 저장해 둔 Threads 글 {n}건의 요약이다.

{corpus}

이 사람에게 맞는 분류 체계를 설계해라. 출력 형식:
{{"categories": [
  {{"slug": "영문-소문자-하이픈", "name": "한국어 이름", "description": "어떤 글이 여기 오는지 한 문장", "view": "card|table|board"}}
]}}"""


def _slugify(s: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9\-]+", "-", (s or "").lower()).strip("-")
    return s or fallback


def sample_corpus(limit: int = 150, chars: int = 320) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, author, full_text, body FROM posts ORDER BY saved_at DESC"
        ).fetchall()
    posts = [dict(r) for r in rows]
    if len(posts) > limit:  # 고르게 표본 추출
        step = len(posts) / limit
        posts = [posts[int(i * step)] for i in range(limit)]
    out = []
    for p in posts:
        text = (p["full_text"] or p["body"] or "").strip().replace("\n", " ")
        if not text:
            continue
        out.append({"id": p["id"], "author": p["author"], "text": text[:chars]})
    return out


def build(force: bool = False) -> list[dict]:
    """카테고리 체계를 생성/재구성해서 DB에 저장."""
    with db() as conn:
        existing = conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"]
    if existing and not force:
        log.info("이미 카테고리가 %d개 있습니다 (force=False)", existing)
        with db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY sort")]

    corpus = sample_corpus()
    if not corpus:
        raise RuntimeError("분류할 글이 없습니다. 먼저 수집해 주세요.")

    lines = [f"[{i+1}] @{c['author']}: {c['text']}" for i, c in enumerate(corpus)]
    data = llm.ask_json(
        SYSTEM,
        TEMPLATE.format(n=len(corpus), corpus="\n".join(lines)),
        max_tokens=3000,
        temperature=0.3,
    )
    cats = data.get("categories") or []
    if not cats:
        raise RuntimeError("카테고리 생성 결과가 비어 있습니다.")

    if not any(_slugify(c.get("slug", ""), "") in {"etc", "misc", "other"} or c.get("name") == "기타"
               for c in cats):
        cats.append({"slug": "etc", "name": "기타", "description": "다른 분류에 맞지 않는 글",
                     "view": "card"})

    with db() as conn:
        if force:
            conn.execute("DELETE FROM categories")
        for i, c in enumerate(cats):
            slug = _slugify(c.get("slug", ""), f"cat-{i+1}")
            conn.execute(
                """INSERT INTO categories(slug,name,description,color,view,sort,created_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name, description=excluded.description, view=excluded.view""",
                (slug, c.get("name") or slug, c.get("description") or "",
                 PALETTE[i % len(PALETTE)],
                 c.get("view") if c.get("view") in ("card", "table", "board") else "card",
                 (999 if c.get("name") == "기타" else i), now()),
            )
        rows = [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY sort")]
    log.info("카테고리 %d개 생성", len(rows))
    return rows
