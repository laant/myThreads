"""두 번째 실행은 할 일이 없어야 한다.

동기화가 끝난 직후 다시 돌렸는데 '상세 수집 6/35건' 처럼 다시 처리하기 시작하면,
저장하지 '않기로' 한 글(남의 글에 단 댓글 등)을 기억하지 않고 있다는 뜻이다.
그런 글은 DB에 남지 않으므로 매번 새 글로 보이고, 매번 다시 열어보게 된다.
"""
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

from app import config, pipeline  # noqa: E402
from app.collector import scraper  # noqa: E402
from app.db import db, init_db  # noqa: E402

COMMENT_EVERY = 3          # 3개 중 1개는 '남의 글에 단 댓글'을 저장해 둔 것
_orig_make_post = base.make_post


def make_post_mixed(idx: int) -> dict:
    p = _orig_make_post(idx)
    if idx % COMMENT_EVERY == 0 and idx > 0:
        p["text_post_app_info"]["reply_to_author"] = {"username": "someone_else"}
    return p


async def run_once() -> dict:
    stats = {"신규": 0}
    processed = {"n": 0}

    async def on_post(item):
        await pipeline.save_post(item)
        processed["n"] += 1

    async def on_skip(item, reason):
        from app.db import mark_skipped
        mark_skipped(item.get("id"), reason, item.get("author"), item.get("posted_at"))

    async def progress(done, total):
        stats["total"] = total

    await scraper.collect(
        known_ids=pipeline.known_post_ids(), known_keys=pipeline.known_post_keys(),
        on_post=on_post, on_skip=on_skip, progress=progress, stats=stats,
    )
    stats["처리"] = processed["n"]
    return stats


def main() -> int:
    base.make_post = make_post_mixed
    srv = ThreadingHTTPServer(("127.0.0.1", 8135), base.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8135/saved"
    config.BASE_URL = "http://127.0.0.1:8135"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 40, 350, 3
    config.DOWNLOAD_MEDIA = False
    config.SKIP_REPLIES = True

    first = asyncio.run(run_once())
    comments = len([i for i in range(1, base.TOTAL) if i % COMMENT_EVERY == 0])
    print(f"1차: 처리 {first['처리']}건 · 제외(댓글) {first.get('제외(댓글)')}건")
    assert first["처리"] == base.TOTAL - comments, first

    second = asyncio.run(run_once())
    print(f"2차: 처리 {second['처리']}건 (기대 0) · 대상 {second.get('total', 0)}건")
    srv.shutdown()
    base.make_post = _orig_make_post

    assert second["처리"] == 0, (
        f"두 번째 실행에서 {second['처리']}건을 다시 처리했습니다 — "
        "제외한 글을 기억하지 못하고 있습니다")
    assert second.get("total", 0) == 0, f"할 일이 {second['total']}건 남아 있음"

    with db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        sk = conn.execute("SELECT COUNT(*) c FROM skipped").fetchone()["c"]
    print(f"✓ 두 번째 실행은 할 일 0건 (저장 {n}건 · 건너뛴 기록 {sk}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
