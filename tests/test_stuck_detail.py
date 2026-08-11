"""'상세 수집 3/25건에서 멈춤' 재현 및 방지 테스트.

응답을 끝내지 않는 상세 페이지가 하나 끼면, 그 글에서 영원히 멈출 수 있다.
더 나쁜 건 밖에서 asyncio.wait_for 로 잘라내는 방식인데, 브라우저 연결이
어중간한 상태로 남아 '그 다음 글부터 전부' 멈춰버린다.
여기서는 그런 페이지를 3번째 글에 심어두고, 전체가 끝까지 진행되는지 본다.
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

import test_scroll_e2e as base  # noqa: E402

from app import config  # noqa: E402
from app.collector import scraper  # noqa: E402

STUCK_IDX = 2          # 3번째 글의 상세 페이지가 응답을 끝내지 않는다
_orig_make_post = base.make_post


def make_post_needing_detail(idx: int) -> dict:
    p = _orig_make_post(idx)
    p["text_post_app_info"]["direct_reply_count"] = 2      # 상세를 열게 만든다
    return p


OK_HTML = b"<!doctype html><meta charset='utf-8'><body>ok</body>"


class Handler(base.Handler):
    def do_GET(self):
        if "/post/" in self.path:
            code = self.path.split("/post/")[-1]
            if code == f"POST{STUCK_IDX:03d}":
                # 헤더만 보내고 본문을 끝내지 않는다 → 로딩이 안 끝나는 페이지
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", "999999")
                self.end_headers()
                try:
                    for _ in range(120):
                        self.wfile.write(b"<!-- -->")
                        self.wfile.flush()
                        time.sleep(1)
                except Exception:
                    pass
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(OK_HTML)))
            self.end_headers()
            self.wfile.write(OK_HTML)
            return
        super().do_GET()


def main() -> int:
    base.make_post = make_post_needing_detail
    base.TOTAL, base.PAGES = 9, 1
    srv = ThreadingHTTPServer(("127.0.0.1", 8137), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8137/saved"
    config.BASE_URL = "http://127.0.0.1:8137"
    config.MAX_SCROLLS, config.SCROLL_PAUSE_MS, config.SCROLL_IDLE_ROUNDS = 20, 300, 3
    config.DETAIL_SETTLE_MS, config.DETAIL_SCROLLS, config.DETAIL_PAUSE_MS = 200, 1, 30
    config.DETAIL_TIMEOUT_SEC = 8          # 테스트라 짧게
    config.DOWNLOAD_MEDIA = False
    config.TRUST_LIST_THREADS = False
    config.SKIP_REPLIES = False

    done = []
    progressed = []

    async def on_post(item):
        done.append(item["id"])

    async def progress(a, b):
        progressed.append((a, b))

    t0 = time.time()
    asyncio.run(scraper.collect(known_ids=set(), on_post=on_post, progress=progress))
    elapsed = time.time() - t0
    srv.shutdown()
    base.make_post = _orig_make_post

    print(f"진행 {len(progressed)}/{base.TOTAL}건 · 저장 {len(done)}건 · {elapsed:.0f}초")
    assert len(progressed) == base.TOTAL, (
        f"{len(progressed)}건에서 멈췄습니다 — 안 끝나는 페이지 하나가 전체를 막고 있습니다")
    assert f"POST{STUCK_IDX:03d}" in done, "멈춘 글도 목록 정보로는 저장돼야 합니다"
    assert elapsed < 120, f"너무 오래 걸림 ({elapsed:.0f}초)"
    print("✓ 응답이 안 끝나는 상세 페이지가 있어도 그 글만 건너뛰고 끝까지 진행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
