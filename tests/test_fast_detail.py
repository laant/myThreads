"""상세 페이지 생략 테스트.

목록 응답에 본문·이미지가 이미 다 들어 있고 댓글이 0개면 '작성자가 이어서 쓴 글'이
있을 수 없다. 이 경우 상세 페이지를 열지 않아야 하고(건당 5~10초), 댓글이 있는 글만
열어서 이어쓴 글을 가져와야 한다.
"""
import asyncio
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATA_DIR", "./data-test")

import test_resume_e2e as resume  # noqa: E402
import test_scroll_e2e as base  # noqa: E402

from app import config  # noqa: E402
from app.collector import scraper  # noqa: E402

HAS_REPLIES_EVERY = 6      # 6의 배수 인덱스만 댓글이 있는 글
_orig_make_post = base.make_post
visits: list[str] = []


def make_post_mixed(idx: int) -> dict:
    p = _orig_make_post(idx)
    p["text_post_app_info"]["direct_reply_count"] = 3 if idx % HAS_REPLIES_EVERY == 0 else 0
    return p


class Handler(resume.Handler):
    def do_GET(self):
        if "/post/" in self.path:
            visits.append(self.path)
        super().do_GET()


def test_unit_rules():
    config.SKIP_DETAIL_WHEN_NO_REPLIES = True
    assert scraper.detail_needed({"body": "내용", "reply_count": 0}) is False
    assert scraper.detail_needed({"body": "내용", "reply_count": 2}) is True
    assert scraper.detail_needed({"body": "", "reply_count": 0}) is True   # 본문을 모르면 열어본다
    config.SKIP_DETAIL_WHEN_NO_REPLIES = False
    assert scraper.detail_needed({"body": "내용", "reply_count": 0}) is True
    config.SKIP_DETAIL_WHEN_NO_REPLIES = True
    print("✓ 상세 페이지를 열지 말지 판단하는 규칙")


def main() -> int:
    test_unit_rules()
    base.make_post = make_post_mixed
    srv = ThreadingHTTPServer(("127.0.0.1", 8131), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8131/saved"
    config.BASE_URL = "http://127.0.0.1:8131"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 40, 350, 3
    config.DETAIL_SETTLE_MS, config.DETAIL_SCROLLS, config.DETAIL_PAUSE_MS = 250, 1, 50
    config.DOWNLOAD_MEDIA = False
    # 목록이 스레드 묶음을 안 줄 때의 대체 경로를 검증한다
    config.TRUST_LIST_THREADS = False
    config.SKIP_REPLIES = True

    stats: dict = {}
    t0 = time.time()
    items = asyncio.run(scraper.collect(known_ids=set(), stats=stats))
    elapsed = time.time() - t0
    srv.shutdown()
    base.make_post = _orig_make_post

    with_replies = {f"POST{i:03d}" for i in range(base.TOTAL) if i % HAS_REPLIES_EVERY == 0}
    visited = {v.split("/post/")[-1] for v in visits}

    assert len(items) == base.TOTAL, f"{len(items)}건 수집 (기대 {base.TOTAL})"
    assert visited == with_replies, f"연 페이지가 다름: 추가 {visited - with_replies}"
    assert stats["빠른수집"] == base.TOTAL - len(with_replies), stats
    assert stats["페이지열기"] == len(with_replies), stats

    # 댓글 없는 글도 본문·이미지는 온전해야 한다
    plain = next(i for i in items if i["id"] == "POST007")
    assert plain["body"] == "저장한 글 7 본문"
    assert plain["media"][0]["url"] == "https://cdn/7.jpg"
    assert plain["detail_ok"] == 1

    # 댓글 있는 글은 이어쓴 글이 붙어야 한다
    threaded = next(i for i in items if i["id"] == "POST012")
    assert "이어서 쓴 글 12-1" in threaded["full_text"], threaded["full_text"][:80]
    assert "남이 단 댓글" not in threaded["full_text"]

    print(f"✓ {base.TOTAL}건 중 {len(with_replies)}건만 페이지를 열었습니다 "
          f"(생략 {stats['빠른수집']}건) — {elapsed:.0f}초")
    print(f"  목록 훑기 {stats['목록훑기(초)']}초 · 상세 {stats['상세수집(초)']}초")
    return 0


if __name__ == "__main__":
    sys.exit(main())
