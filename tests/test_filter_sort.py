"""댓글 제외 + 정렬 테스트."""
import asyncio
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATA_DIR", "./data-test")

import test_scroll_e2e as base  # noqa: E402

from app import config, main  # noqa: E402
from app.classifier.classify import _clean_tags  # noqa: E402
from app.collector import scraper  # noqa: E402
from app.db import db, init_db, now, upsert_post  # noqa: E402

COMMENT_EVERY = 5          # 5의 배수 인덱스는 '남의 글에 단 댓글'로 만든다
_orig_make_post = base.make_post


def make_post_with_comments(idx: int) -> dict:
    p = _orig_make_post(idx)
    if idx % COMMENT_EVERY == 0 and idx > 0:
        p["text_post_app_info"]["reply_to_author"] = {"username": "someone_else"}
    return p


def test_is_comment_rules():
    assert scraper.is_comment({"author": "me", "reply_to": "other"}) is True
    assert scraper.is_comment({"author": "me", "reply_to": "me"}) is False   # 내 스레드 이어쓰기
    assert scraper.is_comment({"author": "me", "reply_to": ""}) is False
    assert scraper.is_comment({"author": "me"}) is False
    print("✓ 댓글 판별 규칙")


def test_tag_cleanup():
    assert _clean_tags(["댓글", "마케팅", "#카피", "reply", "마케팅"]) == ["마케팅", "카피"]
    assert _clean_tags(None) == []
    print("✓ '댓글' 같은 형식 태그 제거")


def test_collect_excludes_comments():
    base.make_post = make_post_with_comments
    srv = ThreadingHTTPServer(("127.0.0.1", 8127), base.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8127/saved"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 40, 350, 3
    config.SKIP_REPLIES = True

    items = asyncio.run(scraper.collect(fetch_details=False))
    srv.shutdown()
    base.make_post = _orig_make_post

    codes = {i["id"] for i in items}
    comments = {f"POST{i:03d}" for i in range(1, base.TOTAL) if i % COMMENT_EVERY == 0}
    assert not (codes & comments), f"댓글이 섞임: {sorted(codes & comments)[:5]}"
    assert len(codes) == base.TOTAL - len(comments), f"{len(codes)}건"
    # 저장 순서(랭크)가 매겨졌는지
    ranks = [i["saved_rank"] for i in items if i.get("saved_rank") is not None]
    assert len(ranks) == len(items) and min(ranks) == 0
    print(f"✓ 댓글 {len(comments)}건 제외, 원글 {len(codes)}건만 수집 + 저장순 부여")


def test_sorting():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()
    rows = [
        # id,   posted_at(작성일),  saved_rank(저장순), author
        ("A", 1000, 2, "kim"),
        ("B", 3000, 0, "park"),
        ("C", 2000, 1, "an"),
        ("D", 0, 3, "zoo"),      # 작성일 모름
    ]
    with db() as conn:
        for pid, posted, rank, author in rows:
            upsert_post(conn, {"id": pid, "posted_at": posted, "saved_rank": rank,
                               "author": author, "body": pid, "full_text": pid,
                               "detail_ok": 1})
            conn.execute("UPDATE posts SET saved_at=? WHERE id=?", (now(), pid))

    order = lambda s: [p["id"] for p in main._posts(sort=s)]  # noqa: E731
    assert order("newest") == ["B", "C", "A", "D"], order("newest")
    assert order("oldest") == ["A", "C", "B", "D"], order("oldest")
    assert order("saved") == ["B", "C", "A", "D"], order("saved")
    assert order("author") == ["C", "A", "B", "D"], order("author")
    print("✓ 최신순 / 오래된순 / 최근 저장순 / 작성자순 정렬")


if __name__ == "__main__":
    test_is_comment_rules()
    test_tag_cleanup()
    test_collect_excludes_comments()
    test_sorting()
    print("댓글 제외 · 정렬 테스트 통과")
