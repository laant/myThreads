"""'매번 같은 글을 다시 연다' 문제 재현 및 방지 테스트.

상세 페이지에서 아무것도 못 건지면 그 글은 detail_ok=0 으로 저장된다. 그러면
다음 실행에서 '아직 안 한 글'로 보여 또 열게 되고, 이게 반복되면 동기화가
끝난 직후 다시 돌려도 '상세 수집 6/35건' 처럼 계속 일감이 생긴다.
실패도 세어서 몇 번 뒤에는 포기해야 한다.
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
from app.db import db, init_db, mark_skipped  # noqa: E402

_orig_make_post = base.make_post
EMPTY_HTML = b"<!doctype html><meta charset='utf-8'><body>no data here</body>"


def make_post_needing_detail(idx: int) -> dict:
    """댓글이 있다고 표시 → 상세 페이지를 열어야 하는 글."""
    p = _orig_make_post(idx)
    p["text_post_app_info"]["direct_reply_count"] = 2
    return p


class Handler(base.Handler):
    """상세 페이지가 아무 데이터도 주지 않는 상황 (Threads UI 변경 등)."""

    def do_GET(self):
        if "/post/" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(EMPTY_HTML)))
            self.end_headers()
            self.wfile.write(EMPTY_HTML)
            return
        super().do_GET()


async def run_once() -> int:
    """이번 실행에서 상세 페이지를 몇 번 열었는지 반환."""
    stats: dict = {}

    async def on_post(item):
        await pipeline.save_post(item)

    async def on_skip(item, reason):
        mark_skipped(item.get("id"), reason, item.get("author"), item.get("posted_at"))

    await scraper.collect(
        known_ids=pipeline.known_post_ids(), known_keys=pipeline.known_post_keys(),
        on_post=on_post, on_skip=on_skip, stats=stats,
    )
    return int(stats.get("페이지열기", 0))


def main() -> int:
    base.make_post = make_post_needing_detail
    base.TOTAL, base.PAGES = 9, 1          # 작게 — 반복 실행이 목적
    srv = ThreadingHTTPServer(("127.0.0.1", 8136), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    init_db()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8136/saved"
    config.BASE_URL = "http://127.0.0.1:8136"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 20, 300, 3
    config.DETAIL_SETTLE_MS, config.DETAIL_SCROLLS, config.DETAIL_PAUSE_MS = 200, 1, 30
    config.DOWNLOAD_MEDIA = False
    config.TRUST_LIST_THREADS = False      # 묶음이 없어 상세를 열어야 하는 상황

    opens = [asyncio.run(run_once()) for _ in range(4)]
    srv.shutdown()
    base.make_post = _orig_make_post

    print(f"실행별 상세 페이지 연 횟수: {opens}")
    assert opens[0] > 0, "첫 실행에서는 열어봐야 한다"
    assert opens[-1] == 0, f"네 번째 실행에서도 {opens[-1]}건을 또 열고 있습니다"
    assert opens[1] <= opens[0], "재시도가 줄지 않습니다"

    with db() as conn:
        sk = conn.execute(
            "SELECT COUNT(*) c FROM skipped WHERE reason='failed'").fetchone()["c"]
    print(f"✓ 실패한 글을 기억해 재시도를 멈춤 (기록 {sk}건) — "
          f"동기화 직후 다시 돌려도 할 일 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
