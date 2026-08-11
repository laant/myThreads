"""목록 응답만으로 전문을 구성하는지 검증 (상세 페이지 0회).

Threads 목록 응답은 글을 thread_items 배열로 준다. 작성자가 이어서 쓴 글이 있으면
그 배열에 함께 들어 있으므로, 목록만 훑어도 '본문 + 이어쓴 글 + 이미지'가 완성된다.
상세 페이지를 여는 것은 이미 받은 데이터를 버리고 다시 받는 셈이다.
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
from app.collector import parser, scraper  # noqa: E402

CONT_EVERY = 4          # 4의 배수 인덱스는 작성자가 이어서 쓴 글이 2개 있다
visits: list[str] = []


def thread_group(idx: int) -> list[dict]:
    """실제 Threads 목록 응답처럼 thread_items 배열을 만든다."""
    root = base._orig_make_post(idx) if hasattr(base, "_orig_make_post") else base.make_post(idx)
    author = root["user"]["username"]
    items = [{"post": root}]
    if idx % CONT_EVERY == 0:
        root["text_post_app_info"]["direct_reply_count"] = 5   # 남의 댓글도 있다고 가정
        for n in (1, 2):
            p = base.make_post(idx * 100 + n)
            p["code"] = f"POST{idx:03d}C{n}"
            p["user"]["username"] = author
            p["caption"] = {"text": f"이어서 쓴 글 {idx}-{n}"}
            p["taken_at"] = root["taken_at"] + n
            p["text_post_app_info"]["reply_to_author"] = {"username": author}
            p.pop("image_versions2", None)
            items.append({"post": p})
    return items


def page_payload(page: int) -> dict:
    start = page * base.PER_PAGE
    return {"data": {"feedback": {"edges": [
        {"node": {"thread_items": thread_group(i)}}
        for i in range(start, min(start + base.PER_PAGE, base.TOTAL))
    ]}}}


class Handler(resume.Handler):
    def do_GET(self):
        if "/post/" in self.path:
            visits.append(self.path)
        super().do_GET()


def main() -> int:
    base.page_payload = page_payload          # 목록 응답을 스레드 묶음 형태로 교체
    srv = ThreadingHTTPServer(("127.0.0.1", 8133), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8133/saved"
    config.BASE_URL = "http://127.0.0.1:8133"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 40, 350, 3
    config.DETAIL_SETTLE_MS, config.DETAIL_SCROLLS, config.DETAIL_PAUSE_MS = 250, 1, 50
    config.DOWNLOAD_MEDIA = False
    config.SKIP_REPLIES = True
    config.TRUST_LIST_THREADS = True

    stats: dict = {}
    t0 = time.time()
    items = asyncio.run(scraper.collect(known_ids=set(), stats=stats))
    elapsed = time.time() - t0
    srv.shutdown()

    assert len(items) == base.TOTAL, f"{len(items)}건 (기대 {base.TOTAL})"
    assert not visits, f"상세 페이지를 {len(visits)}번 열었습니다: {visits[:3]}"
    assert stats["페이지열기"] == 0, stats

    # 이어쓴 글이 있는 글 — 목록만으로 전문이 만들어져야 한다
    threaded = next(i for i in items if i["id"] == "POST012")
    assert "이어서 쓴 글 12-1" in threaded["full_text"], threaded["full_text"][:120]
    assert "이어서 쓴 글 12-2" in threaded["full_text"]
    assert [s["kind"] for s in threaded["segments"]] == ["root", "reply", "reply"]
    assert threaded["detail_ok"] == 1

    # 이어쓴 글이 없는 글도 본문·이미지가 온전해야 한다
    plain = next(i for i in items if i["id"] == "POST007")
    assert plain["body"] == "저장한 글 7 본문"
    assert plain["media"][0]["url"] == "https://cdn/7.jpg"
    assert plain["thread_text"] == ""

    print(f"✓ {base.TOTAL}건 전부 목록만으로 수집 — 상세 페이지 {len(visits)}회, {elapsed:.0f}초")
    print(f"  목록 훑기 {stats['목록훑기(초)']}초 · 나머지 {stats['상세수집(초)']}초")
    return 0


def test_walk_threads_unit():
    payload = {"data": {"x": {"edges": [{"node": {"thread_items": thread_group(4)}}]}}}
    groups = parser.walk_threads(payload)
    assert len(groups) == 1 and len(groups[0]) == 3, [len(g) for g in groups]
    items = [parser.normalize(n) for n in groups[0]]
    t = parser.build_thread(items, items[0]["id"])
    assert "이어서 쓴 글 4-1" in t["thread_text"]
    print("✓ thread_items 묶음에서 본문 + 이어쓴 글 복원")


if __name__ == "__main__":
    test_walk_threads_unit()
    sys.exit(main())
