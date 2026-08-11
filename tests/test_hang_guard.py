"""멈춤 방지 테스트.

Threads는 응답이 끝나지 않는 스트리밍/롱폴링 연결을 유지한다. 예전 코드는 그런
응답의 본문을 기다리다가 영원히 멈췄다. 여기서는 '절대 끝나지 않는 JSON 응답'을
섞어 놓고도 수집이 정상적으로 끝나는지, 그리고 글이 한 건씩 즉시 저장되는지 검증한다.
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
os.environ.setdefault("DATA_DIR", "./data")

import test_scroll_e2e as base  # noqa: E402

from app import config  # noqa: E402
from app.collector import scraper  # noqa: E402


class Handler(base.Handler):
    def do_GET(self):
        if self.path.startswith("/api/v1/stream"):
            # 헤더만 보내고 본문을 끝내지 않는 응답 (롱폴링 흉내)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "999999")
            self.end_headers()
            try:
                for _ in range(120):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(1)
            except Exception:
                pass
            return
        super().do_GET()


HTML_WITH_STREAM = base.HTML.replace(
    "loadMore();",
    "loadMore(); fetch('/api/v1/stream').catch(() => {});",
    1,
)


def main() -> int:
    base.HTML = HTML_WITH_STREAM
    srv = ThreadingHTTPServer(("127.0.0.1", 8125), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.STATE_PATH.write_text(json.dumps({"cookies": [], "origins": []}))
    config.SAVED_URL = "http://127.0.0.1:8125/saved"
    config.MAX_SCROLLS = 40
    config.SCROLL_PAUSE_MS = 400
    config.SCROLL_IDLE_ROUNDS = 3
    config.RESPONSE_READ_SEC = 3
    config.DRAIN_TIMEOUT_SEC = 4

    started = time.time()
    items = asyncio.run(scraper.collect(fetch_details=False))
    elapsed = time.time() - started
    srv.shutdown()

    print(f"수집 {len(items)}건 / {elapsed:.0f}초")
    assert len(items) >= base.TOTAL, f"기대 {base.TOTAL}, 실제 {len(items)}"
    assert elapsed < 180, f"너무 오래 걸림({elapsed:.0f}초) — 멈춤 가능성"
    print("✓ 끝나지 않는 응답이 섞여 있어도 수집이 정상 종료됨")
    return 0


if __name__ == "__main__":
    sys.exit(main())
